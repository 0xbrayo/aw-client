import json
import logging
import socket
import appdirs
import os
import time
import threading
from collections import defaultdict
from typing import Optional, List, Union

import requests as req

from aw_core.models import Event
from . import config

# FIXME: This line is probably badly placed
logging.getLogger("requests").setLevel(logging.WARNING)


# TODO: Make resilient to server crashes/offline connection by storing unsent data locally
#       (temporarily until server is up)

# TODO: Should probably use OAuth or something

class ActivityWatchClient:
    def __init__(self, client_name: str, testing=False):
        self.logger = logging.getLogger("aw-client")
        self.testing = testing

        self.session = {}

        self.client_name = client_name
        self.client_hostname = socket.gethostname()

        self.server_hostname = config["server_hostname"] if not testing else config["testserver_hostname"]
        self.server_port = config["server_port"] if not testing else config["testserver_port"]

        # Setup failed queues dir
        self.data_dir = appdirs.user_data_dir("aw-client")
        self.failed_queues_dir = "{}/failed_events/{}/".format(self.data_dir, self.client_name)
        if not os.path.exists(self.failed_queues_dir):
            os.makedirs(self.failed_queues_dir)
        
        # Send old failed events
        QueueTimerThread(self).start()

    def __enter__(self):
        # Should: be used to send a new-session message with eventual client settings etc.
        # Should: Be used to generate a unique session-id to identify the session (hash(time.time() + client_name))
        # Could: be used to generate a session key/authentication
        self._start_session()
        if self.session:
            self.logger.info("Started session")
        else:
            self.logger.error("Failed to start session")

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Should: be used to send a end-session message
        # Could: be used to tell server to discard the session key
        self.session_active = False

    def _start_session(self):
        session_id = "{}#{}".format(self.client_name, int(time.time() * 1000))
        try:
            resp = self._post("session/start", {"session_id": session_id})
            data = resp.json()
            self.session = {"session_id": session_id, "session_key": data["session_key"]}
        except req.RequestException as e:
            self.logger.error("Could not start session: {}".format(e))

    def _stop_session(self):
        assert self.session_active
        resp = self._post("session/stop", self.session)
        if resp:
            self.session = {}

    def _queue_failed_event(self, bucket: str, data: dict):
        # Find failed queue file
        endpoint = 'buckets/{}/events'.format(bucket)
        queue_file = "{d}/{f}".format(d=self.failed_queues_dir, f=bucket)
        with open(queue_file, "a+") as queue_fp:
            queue_fp.write(json.dumps(data)+"\n")

    def _post_failed_events(self):
        failed_events = []
        for bucket in os.listdir(self.failed_queues_dir):
            queue_file_path = "{}/{}".format(self.failed_queues_dir, bucket)
            with open(queue_file_path, "r") as queue_fp:
                for event in queue_fp:
                    failed_events.append(Event(**json.loads(event)))
                self.logger.info("Sent failed events: {}".format(failed_events))
            open(queue_file_path, "w").close() # Truncate file
            self.send_events(bucket, failed_events)

    def _post(self, endpoint: str, data: dict) -> Optional[req.Response]:
        headers = {"Content-type": "application/json"}
        # FIXME: Use HTTPS whenever possible!
        url = "http://{}:{}/api/0/{}".format(self.server_hostname, self.server_port, endpoint)
        response = req.post(url, data=json.dumps(data), headers=headers)
        response.raise_for_status()
        return response

    def _get(self, endpoint: str) -> Optional[req.Response]:
        url = "http://{}:{}/api/0/{}".format(self.server_hostname, self.server_port, endpoint)
        response = req.get(url)
        response.raise_for_status()
        return response
    
    def send_event(self, bucket, event: Union[Event, List[Event]]):
        # TODO: Notice if server responds with invalid session and create a new one
        endpoint = "buckets/{}/events".format(bucket)
        data = event.to_json_dict()
        try:
            self._post(endpoint, data)
            self.logger.debug("Sent event to server: {}".format(event))
        except req.RequestException as e:
            self.logger.warning("Failed to send event to server ({})".format(e))
            self._queue_failed_event(bucket, data)

    def send_events(self, bucket, events: List[Event]):
        # TODO: Notice if server responds with invalid session and create a new one
        endpoint = "buckets/{}/events".format(bucket)
        data = [event.to_json_dict() for event in events]
        try:
            self._post(endpoint, data)
            self.logger.debug("Sent events to server: {}".format(events))
        except req.RequestException as e:
            self.logger.warning("Failed to send events to server ({})".format(e))
            for event in data:
                self._queue_failed_event(bucket, event)

    def get_buckets(self):
        return self._get('buckets')

    def create_bucket(self, bucket_id, event_type: str):
        # Check if bucket exists
        buckets = self.get_buckets()
        if bucket_id in buckets:
            return False  # Don't do anything if bucket already exists
        else:
            # Create bucket
            endpoint = "buckets/{}".format(bucket_id)
            data = {
                'client': self.client_name,
                'hostname': self.client_hostname,
                'type': event_type,
            }
            try:
                self._post(endpoint, data)
            except req.RequestException as e:
                self.logger.error("Unable to create bucket: {}".format(e))
            return True



class QueueTimerThread(threading.Thread):
    def __init__(self, aw_client):
        threading.Thread.__init__(self)
        self.daemon = True
        self.aw_client = aw_client

    def run(self):
        while True:
            self.aw_client._post_failed_events()
            time.sleep(180)

