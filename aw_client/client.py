import json
import logging
import socket
import os
import time
import threading
from collections import namedtuple
from typing import Optional, List
from queue import Queue

import requests as req

from aw_core.models import Event
from aw_core.dirs import get_data_dir

from .config import load_config


# FIXME: This line is probably badly placed
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("aw.client")


# TODO: Should probably use OAuth or something

class ActivityWatchClient:
    def __init__(self, client_name: str, testing=False) -> None:
        self.testing = testing

        self.buckets = []  # type: List[Dict[str, str]]
        # self.session = {}

        self.client_name = client_name + ("-testing" if testing else "")
        self.client_hostname = socket.gethostname()

        client_config = load_config()
        configsection = "server" if not testing else "server-testing"

        self.server_hostname = client_config[configsection]["hostname"]
        self.server_port = client_config[configsection]["port"]

        self.dispatch_thread = PostDispatchThread(self)

    #
    #   Get/Post base requests
    #

    def _post(self, endpoint: str, data: dict) -> Optional[req.Response]:
        headers = {"Content-type": "application/json"}
        url = "http://{}:{}/api/0/{}".format(self.server_hostname, self.server_port, endpoint)
        response = req.post(url, data=json.dumps(data), headers=headers)
        response.raise_for_status()
        return response

    def _get(self, endpoint: str) -> Optional[req.Response]:
        url = "http://{}:{}/api/0/{}".format(self.server_hostname, self.server_port, endpoint)
        response = req.get(url)
        response.raise_for_status()
        return response

    #
    #   Event get/post requests
    #

    def get_events(self, bucket: str) -> List[Event]:
        endpoint = "buckets/{}/events".format(bucket)
        events = self._get(endpoint).json()
        return [Event(**event) for event in events]

    def send_event(self, bucket: str, event: Event):
        endpoint = "buckets/{}/events".format(bucket)
        data = event.to_json_dict()
        self.dispatch_thread.add_request(endpoint, data)

    def send_events(self, bucket: str, events: List[Event], ignore_failed=False):
        endpoint = "buckets/{}/events".format(bucket)
        data = [event.to_json_dict() for event in events]
        self.dispatch_thread.add_request(endpoint, data)

    def replace_last_event(self, bucket: str, event: Event):
        endpoint = "buckets/{}/events/replace_last".format(bucket)
        data = event.to_json_dict()
        self.dispatch_thread.add_request(endpoint, data)

    def heartbeat(self, bucket, event: Event, pulsetime: float):
        endpoint = "buckets/{}/heartbeat?pulsetime={}".format(bucket, pulsetime)
        data = event.to_json_dict()
        self.dispatch_thread.add_request(endpoint, data)

    #
    #   Bucket get/post requests
    #

    def get_buckets(self):
        return self._get('buckets').json()

    def setup_bucket(self, bucket_id: str, event_type: str) -> bool:
        self.buckets.append({"bid": bucket_id, "etype": event_type})

    def _create_buckets(self):
        # Check if bucket exists
        buckets = self.get_buckets()
        for bucket in self.buckets:
            if bucket['bid'] in buckets:
                return False  # Don't do anything if bucket already exists
            else:
                # Create bucket
                endpoint = "buckets/{}".format(bucket['bid'])
                data = {
                    'client': self.client_name,
                    'hostname': self.client_hostname,
                    'type': bucket['etype'],
                }
                self._post(endpoint, data)
                return True

    #
    #   Connection methods
    #

    def connect(self):
        if not self.dispatch_thread.is_alive():
            self.dispatch_thread.start()

    def disconnect(self):
        # FIXME: doesn't disconnect immediately
        self.dispatch_thread.running = False

QueuedRequest = namedtuple("QueuedRequest", ["endpoint", "data"])


class PostDispatchThread(threading.Thread):
    def __init__(self, client):
        threading.Thread.__init__(self, daemon=True)
        self.running = True
        self.connected = False

        self.client = client
        self.queue = Queue()

        # Setup failed queues file
        data_dir = get_data_dir("aw-client")
        failed_queues_dir = os.path.join(data_dir, "failed_events")
        if not os.path.exists(failed_queues_dir):
            os.makedirs(failed_queues_dir)
        self.queue_file = os.path.join(failed_queues_dir, self.client.client_name)

        self._load_queue()
        logger.info("Loaded {} failed events from queue file".format(self.queue.qsize()))

    def _queue_failed_request(self, endpoint: str, data: dict):
        # Find failed queue file
        entry = QueuedRequest(endpoint=endpoint, data=data)
        with open(self.queue_file, "a+") as queue_fp:
            queue_fp.write(json.dumps(entry) + "\n")

    def _load_queue(self):
        # If crash when lost connection, queue failed requests
        failed_requests = []  # type: List[QueuedRequests]
        open(self.queue_file, "a").close()  # Create file if doesn't exist
        with open(self.queue_file, "r") as queue_fp:
            for request in queue_fp:
                logger.debug(request)
                failed_requests.append(QueuedRequest(*json.loads(request)))

        open(self.queue_file, "w").close()  # Clear file
        if len(failed_requests) > 0:
            logger.info("Adding {} failed events to queue to send".format(len(failed_requests)))
            for request in failed_requests:
                self.queue.put(request)

    def _save_queue(self):
        # When lost connection, save queue to file for later sending
        with open(self.queue_file, "w") as queue_fp:
            for request in self.queue.queue:
                queue_fp.write(json.dumps(request) + "\n")

    def run(self):
        while self.running:
            while not self.connected and self.running:
                try:  # Try to connect
                    self.client._create_buckets()
                    self.connected = True
                    logger.warning("Connection to aw-server established")
                except req.RequestException as e:
                    # If unable to connect, retry in 10s
                    time.sleep(40)
            self._load_queue()
            while self.connected and self.running:
                request = self.queue.get()
                try:
                    self.client._post(request.endpoint, request.data)
                except req.RequestException as e:
                    self.queue.queue.appendleft(request)
                    self.connected = False
                    logger.warning("Can't connect to aw-server, will queue events until connection is available: {}".format(e))
            self._save_queue()

    def add_request(self, endpoint, data):
        if self.connected:
            self.queue.put(QueuedRequest(endpoint=endpoint, data=data))
        else:
            self._queue_failed_request(endpoint, data)
