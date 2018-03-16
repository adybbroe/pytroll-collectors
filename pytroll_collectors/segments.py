#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2015, 2016 Panu Lahtinen

# Author(s): Panu Lahtinen

#   Panu Lahtinen <panu.lahtinen@fmi.fi>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Gather GEO stationary segments, or polar satellite granules for one
timestep, and send them in a bunch as a dataset.
"""

import datetime as dt
import logging
import logging.handlers
import os.path
import Queue
import time
from collections import OrderedDict
from urlparse import urlparse, urlunparse

from posttroll import message, publisher
from posttroll.listener import ListenerContainer
from trollsift import Parser, compose

SLOT_NOT_READY = 0
SLOT_NONCRITICAL_NOT_READY = 1
SLOT_READY = 2
SLOT_READY_BUT_WAIT_FOR_MORE = 3
SLOT_OBSOLETE_TIMEOUT = 4

DO_NOT_COPY_KEYS = ("uid", "uri", "channel_name", "segment", "sensor")
REMOVE_TAGS = {'path', 'segment'}


class SegmentGatherer(object):

    """Gatherer for geostationary satellite segments and multifile polar
    satellite granules."""

    def __init__(self, config):
        self._config = config
        topics = config['posttroll'].get('topics')
        addresses = config['posttroll'].get('addresses')
        publish_port = config['posttroll'].get('publish_port', 0)
        nameservers = config['posttroll'].get('nameservers', [])

        self._listener = ListenerContainer(topics=topics, addresses=addresses)
        self._publisher = publisher.NoisyPublisher("segment_gatherer",
                                                   port=publish_port,
                                                   nameservers=nameservers)
        self._subject = config['posttroll']['publish_topic']

        self._patterns = config['patterns']

        self._time_tolerance = config.get("time_tolerance", 30)
        self._timeliness = dt.timedelta(seconds=config.get("timeliness", 1200))

        self._num_files_premature_publish = \
            config.get("num_files_premature_publish", -1)

        self.slots = OrderedDict()

        self._parsers = {key: Parser(self._patterns[key]['pattern']) for
                         key in self._patterns}

        self.time_name = config.get('time_name', 'start_time')

        self.logger = logging.getLogger("segment_gatherer")
        self._loop = False

    def _clear_data(self, time_slot):
        """Clear data."""
        if time_slot in self.slots:
            del self.slots[time_slot]

    def _init_data(self, mda):
        """Init wanted, all and critical files"""
        # Init metadata struct
        metadata = mda.copy()
        metadata['dataset'] = []

        time_slot = str(metadata[self.time_name])
        self.logger.debug("Adding new slot: %s", time_slot)
        self.slots[time_slot] = {}
        slot = self.slots[time_slot]
        slot['metadata'] = metadata.copy()
        slot['timeout'] = None

        # Critical files that are required, otherwise production will fail.
        # If there are no critical files, empty set([]) is used.
        patterns = self._config['patterns']
        for key in patterns:
            self.slots[time_slot][key] = {}
            slot = self.slots[time_slot][key]
            is_critical_set = patterns[key].get("is_critical_set", False)
            slot['is_critical_set'] = is_critical_set
            slot['critical_files'] = set([])
            slot['wanted_files'] = set([])
            slot['all_files'] = set([])
            slot['received_files'] = set([])
            slot['delayed_files'] = dict()
            slot['missing_files'] = set([])
            slot['files_till_premature_publish'] = \
                    self._num_files_premature_publish

            critical_segments = patterns[key].get("critical_files", None)
            if critical_segments:
                fname_set = self._compose_filenames(key, time_slot,
                                                    critical_segments)
                slot['critical_files'].update(fname_set)

            else:
                fname_set = self._compose_filenames(key, time_slot, ':')
                if is_critical_set:
                    # If critical segments are not defined, but the
                    # file based on this pattern is required, add it
                    # to critical files
                    slot['critical_files'].update(fname_set)

                # In any case add it to the wanted and all files
                slot['wanted_files'].update(fname_set)
                slot['all_files'].update(fname_set)

            # These segments are wanted, but not critical to production
            wanted_segments = patterns[key].get("wanted_files", ':')
            slot['wanted_files'].update(
                self._compose_filenames(key, time_slot, wanted_segments))

            # Name of all the files
            all_segments = patterns[key].get("all_files", None)
            if all_segments:
                fname_set = self._compose_filenames(key, time_slot,
                                                    all_segments)
                slot['all_files'].update(fname_set)


    def _compose_filenames(self, key, time_slot, itm_str):
        """Compose filename set()s based on a pattern and item string.
        itm_str is formated like ':PRO,:EPI' or 'VIS006:8,VIS008:1-8,...'"""

        # Empty set
        result = set()

        # Get copy of metadata
        meta = self.slots[time_slot]['metadata'].copy()

        # Replace variable tags (such as processing time) with
        # wildcards, as these can't be forecasted.
        var_tags = self._config['patterns'][key].get('variable_tags', [])
        meta = _copy_without_ignore_items(meta,
                                          ignored_keys=var_tags)

        for itm in itm_str.split(','):
            channel_name, segments = itm.split(':')
            segments = segments.split('-')
            if len(segments) > 1:
                format_string = '%d'
                if len(segments[0]) > 1 and segments[0][0] == '0':
                    format_string = '%0' + str(len(segments[0])) + 'd'
                segments = [format_string % i
                            for i in range(int(segments[0]),
                                           int(segments[-1]) + 1)]
            meta['channel_name'] = channel_name
            for seg in segments:
                meta['segment'] = seg
                fname = self._parsers[key].globify(meta)
                result.add(fname)

        return result

    def _publish(self, time_slot, missing_files_check=True):
        """Publish file dataset and reinitialize gatherer."""

        data = self.slots[time_slot]

        # Diagnostic logging about delayed ...
        delayed_files = {}
        for key in self._parsers:
            delayed_files.update(data[key]['delayed_files'])
        if len(delayed_files) > 0:
            file_str = ''
            for key in delayed_files:
                file_str += "%s %f seconds, " % (key, delayed_files[key])
            self.logger.warning("Files received late: %s",
                                file_str.strip(', '))

        # ... and missing files
        if missing_files_check:
            missing_files = set([])
            for key in self._parsers:
                missing_files = data[key]['all_files'].difference(
                    data[key]['received_files'])
            if len(missing_files) > 0:
                self.logger.warning("Missing files: %s",
                                    ', '.join(missing_files))

        # Remove tags that are not necessary for datasets
        for tag in REMOVE_TAGS:
            try:
                del data['metadata'][tag]
            except KeyError:
                pass

        msg = message.Message(self._subject, "dataset", data['metadata'])
        self.logger.info("Sending: %s", str(msg))
        self._publisher.send(str(msg))

        # self._clear_data(time_slot)

    def set_logger(self, logger):
        """Set logger."""
        self.logger = logger

    def update_timeout(self, time_slot):
        timeout = dt.datetime.utcnow() + self._timeliness
        self.slots[time_slot]['timeout'] = timeout
        self.logger.info("Setting timeout to %s for slot %s.",
                         str(timeout), time_slot)

    def slot_ready(self, time_slot):
        """Determine if slot is ready to be published."""
        slot = self.slots[time_slot]

        if slot['timeout'] is None:
            self.update_timeout(time_slot)
            return SLOT_NOT_READY

        status = {}
        num_files = {}
        for key in self._parsers:
            # Default
            status[key] = SLOT_NOT_READY
            if not slot[key]['is_critical_set']:
                status[key] = SLOT_NONCRITICAL_NOT_READY
            #if len(slot[key]['received_files']) == 0:
                # and
                # slot[key]['is_critical_set']):
            #    status[key] = SLOT_NOT_READY

            wanted_and_critical_files = slot[key][
                'wanted_files'].union(slot[key]['critical_files'])
            num_wanted_and_critical = len(
                wanted_and_critical_files & slot[key]['received_files'])

            num_files[key] = num_wanted_and_critical

            if num_wanted_and_critical == \
               slot[key]['files_till_premature_publish']:
                slot[key]['files_till_premature_publish'] = -1
                status[key] = SLOT_READY_BUT_WAIT_FOR_MORE

            if wanted_and_critical_files.issubset(
                    slot[key]['received_files']):
                status[key] = SLOT_READY

#            if slot[key]['critical_files'].issubset(slot[key]['received_files']):
#                status[key] = SLOT_READY

        # Determine overall status
        return self.get_collection_status(status, slot['timeout'], time_slot)

    def get_collection_status(self, status, timeout, time_slot):
        """Determine the overall status of the collection"""
        if len(status) == 0:
            return SLOT_NOT_READY

        if dt.datetime.utcnow() > timeout:
            if SLOT_NONCRITICAL_NOT_READY in status.values():
                return SLOT_READY
            else:
                self.logger.warning("Timeout occured and required files "
                                    "were not present, data discarded for "
                                    "slot %s.",
                                    time_slot)
                return SLOT_OBSOLETE_TIMEOUT

        if SLOT_NOT_READY in status.values():
            return SLOT_NOT_READY
        if SLOT_NONCRITICAL_NOT_READY in status.values():
            return SLOT_NONCRITICAL_NOT_READY
        if all([val == SLOT_READY for val in status.values()]):
            self.logger.info("Timeout occured, required files received "
                             "for slot %s.", time_slot)
            return SLOT_READY
        if SLOT_READY_BUT_WAIT_FOR_MORE in status.values():
            return SLOT_READY_BUT_WAIT_FOR_MORE


    def run(self):
        """Run SegmentGatherer"""
        self._publisher.start()
        self._loop = True
        while self._loop:
            # Check if there are slots ready for publication
            slots = self.slots.copy()
            for slot in slots:
                slot = str(slot)
                status = self.slot_ready(slot)
                if status == SLOT_READY:
                    # Collection ready, publish and remove
                    self._publish(slot)
                    self._clear_data(slot)
                if status == SLOT_READY_BUT_WAIT_FOR_MORE:
                    # Collection ready, publish and but wait for more
                    self._publish(slot, missing_files_check=False)
                elif status == SLOT_OBSOLETE_TIMEOUT:
                    # Collection unfinished and obslote, discard
                    self._clear_data(slot)
                else:
                    # Collection unfinished, wait for more data
                    pass

            # Check listener for new messages
            msg = None
            try:
                msg = self._listener.output_queue.get(True, 1)
            except AttributeError:
                msg = self._listener.queue.get(True, 1)
            except KeyboardInterrupt:
                self.stop()
                continue
            except Queue.Empty:
                continue

            if msg.type == "file":
                self.logger.info("New message received: %s", str(msg))
                self.process(msg)

    def stop(self):
        """Stop gatherer."""
        self.logger.info("Stopping gatherer.")
        self._loop = False
        if self._listener is not None:
            if self._listener.thread is not None:
                self._listener.stop()
        if self._publisher is not None:
            self._publisher.stop()

    def process(self, msg):
        """Process message"""
        mda = None
        # Find the correct parser for this file
        for key in self._config['patterns']:
            parser = self._parsers[key]
            try:
                mda = parser.parse(msg.data["uid"])
                break
            except ValueError:
                continue
        if mda is None:
            self.logger.debug("Unknown file, skipping.")
            return

        metadata = {}

        # Use values parsed from the filename as basis
        for key in mda:
            if key not in DO_NOT_COPY_KEYS:
                metadata[key] = mda[key]

        # Update with data given in the message
        for key in msg.data:
            if key not in DO_NOT_COPY_KEYS:
                metadata[key] = msg.data[key]

        time_slot = self._find_time_slot(metadata["start_time"])

        # Init metadata etc if this is the first file
        if time_slot not in self.slots:
            self._init_data(metadata)

        uri = urlparse(msg.data['uri']).path
        uid = msg.data['uid']

        key = self.key_from_fname(uid)
        slot = self.slots[time_slot][key]
        meta = self.slots[time_slot]['metadata']

        # Check if this file has been received already

        # Replace variable tags (such as processing time) with
        # wildcards, as these can't be forecasted.
        ignored_keys = \
            self._config['patterns'][key].get('variable_tags', [])
        mda = _copy_without_ignore_items(mda,
                                         ignored_keys=ignored_keys)

        mask = self._parsers[key].globify(mda)
        if mask in slot['received_files']:
            return
        if mask not in slot['all_files']:
            return

        # self.update_timeout(time_slot)
        timeout = self.slots[time_slot]['timeout']

        # Add uid and uri
        meta['dataset'].append({'uri': uri, 'uid': uid})

        # Collect all sensors, not only the latest
        if type(msg.data["sensor"]) not in (tuple, list, set):
            msg.data["sensor"] = [msg.data["sensor"]]
        for sensor in msg.data["sensor"]:
            if "sensor" not in meta:
                meta["sensor"] = []
            if sensor not in meta["sensor"]:
                meta["sensor"].append(sensor)

        # If critical files have been received but the slot is
        # not complete, add the file to list of delayed files
        if len(slot['critical_files']) > 0 and \
           slot['critical_files'].issubset(slot['received_files']):
            delay = dt.datetime.utcnow() - (timeout - self._timeliness)
            if delay.total_seconds() > 0:
                slot['delayed_files']['uid'] = delay.total_seconds()

        # Add to received files
        slot['received_files'].add(mask)

    def key_from_fname(self, uid):
        """"""
        for key in self._parsers:
            try:
                _ = self._parsers[key].parse(uid)
                return key
            except ValueError:
                pass

    def _find_time_slot(self, time_obj):
        """Find time slot and return the slot as a string.  If no slots are
        close enough, return *str(time_obj)*"""
        for slot in self.slots:
            time_slot = self.slots[slot]['metadata'][self.time_name]
            time_diff = time_obj - time_slot
            if abs(time_diff.total_seconds()) < self._time_tolerance:
                self.logger.debug("Found existing time slot, using that")
                return str(time_slot)

        return str(time_obj)


def _copy_without_ignore_items(the_dict, ignored_keys=['ignore']):
    """
    get a copy of *the_dict* without entries having substring
    'ignore' in key
    """
    new_dict = {}
    for (key, val) in list(the_dict.items()):
        if key not in ignored_keys:
            new_dict[key] = val
    return new_dict


def arg_parse():
    '''Handle input arguments.
    '''
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--log",
                        help="File to log to (defaults to stdout)",
                        default=None)
    parser.add_argument("-v", "--verbose", help="print debug messages too",
                        action="store_true")
    parser.add_argument("-c", "--config", help="config file to be used")
    parser.add_argument("-C", "--config_item", help="config item to use")

    return parser.parse_args()


def main():
    '''Main. Parse cmdline, read config etc.'''

    args = arg_parse()

    config = RawConfigParser()
    config.read(args.config)

    print "Setting timezone to UTC"
    os.environ["TZ"] = "UTC"
    time.tzset()

    handlers = []
    if args.log:
        handlers.append(
            logging.handlers.TimedRotatingFileHandler(args.log,
                                                      "midnight",
                                                      backupCount=7))

    handlers.append(logging.StreamHandler())

    if args.verbose:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    for handler in handlers:
        handler.setFormatter(logging.Formatter("[%(levelname)s: %(asctime)s :"
                                               " %(name)s] %(message)s",
                                               '%Y-%m-%d %H:%M:%S'))
        handler.setLevel(loglevel)
        logging.getLogger('').setLevel(loglevel)
        logging.getLogger('').addHandler(handler)

    logging.getLogger("posttroll").setLevel(logging.INFO)
    logger = logging.getLogger("segment_gatherer")

    gatherer = SegmentGatherer(config, args.config_item)
    gatherer.set_logger(logger)
    gatherer.run()


if __name__ == "__main__":
    main()
