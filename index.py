import signal
from datetime import datetime
from io import BytesIO

import sqlite3
import numpy as np
import time
import multiprocessing
import cv2
import logging
from collections import defaultdict
import re

from tflite_support.task import core
from tflite_support.task import processor
from tflite_support.task import vision
from tflite_support import metadata_schema_py_generated as _metadata_fb

import paho.mqtt.client as mqtt
import hashlib
import yaml
import sys
import json
import requests
from PIL import Image, ImageOps

config = None
firstmessage = True
_LOGGER = None

VERSION = '1.0.0'

CONFIG_PATH = './config/config.yml'
DB_PATH = './config/frigate_plate_recogizer.db'
LOG_FILE = './config/frigate_plate_recogizer.log'

PLATE_RECOGIZER_BASE_URL = 'https://api.platerecognizer.com/v1/plate-reader'


def on_connect(client, userdata, flags, rc):
    _LOGGER.info("MQTT Connected")
    client.subscribe(config['frigate']['main_topic'] + "/events")


def on_disconnect(client, userdata, rc):
    if rc != 0:
        _LOGGER.warning("Unexpected disconnection, trying to reconnect")
        while True:
            try:
                client.reconnect()
                break
            except Exception as e:
                _LOGGER.warning(f"Reconnection failed due to {e}, retrying in 60 seconds")
                time.sleep(60)
    else:
        _LOGGER.error("Expected disconnection")


def set_sublabel(frigate_url, frigate_event, sublabel):
    post_url = f"{frigate_url}/api/events/{frigate_event}/sub_label"
    _LOGGER.debug(f'sublabel: {sublabel}')
    _LOGGER.debug(f'sublabel url: {post_url}')

    # frigate limits sublabels to 20 characters currently
    if len(sublabel) > 20:
        sublabel = sublabel[:20]

    # Submit the POST request with the JSON payload
    payload = { "subLabel": sublabel }
    headers = { "Content-Type": "application/json" }
    response = requests.post(post_url, data=json.dumps(payload), headers=headers)

    # Check for a successful response
    if response.status_code == 200:
        _LOGGER.info(f"Sublabel set successfully to: {sublabel}")
    else:
        _LOGGER.error(f"Failed to set sublabel. Status code: {response.status_code}")


def on_message(client, userdata, message):
    global firstmessage
    if firstmessage:
        firstmessage = False
        _LOGGER.debug("skipping first message")
        return

    # get frigate event payload
    payload_dict = json.loads(message.payload)
    _LOGGER.debug(f'mqtt message: {payload_dict}')
    after_data = payload_dict.get('after', {})

    if not after_data['camera'] in config['frigate']['camera']:
        _LOGGER.debug(f"Skipping event: {after_data['id']} because it is from the wrong camera: {after_data['camera']}")
        return

    # check if it is a car
    if(after_data['label'] != 'car'):
        _LOGGER.error(f"is not a car label: {after_data['label']}")
        return

    # get frigate event
    frigate_event = after_data['id']
    frigate_url = config['frigate']['frigate_url']

    snapshot_url = f"{frigate_url}/api/events/{frigate_event}/snapshot.jpg"
    _LOGGER.debug(f"Getting image for event: {frigate_event}" )
    _LOGGER.debug(f"event URL: {snapshot_url}")

    response = requests.get(snapshot_url, params={ "crop": 1, "quality": 95 })

    # Check if the request was successful (HTTP status code 200)
    if response.status_code != 200:
        _LOGGER.error(f"Error getting snapshot: {response.status_code}")
        return
    
    # try to get plate number
    token = config['plate_recognizer']['token']
    response = requests.post(
        PLATE_RECOGIZER_BASE_URL,
        regions=dict(regions=config['plate_recognizer']['regions']),
        files=dict(upload=response.content),
        headers={'Authorization': f'Token {token}'}
    )

    response = response.json()
    _LOGGER.debug(f"response: {response}")
    
    plate_number = response['results'][0]['plate']
    score = response['results'][0]['score']

    pprint(response.json())
    return

    start_time = datetime.fromtimestamp(after_data['start_time'])
    formatted_start_time = start_time.strftime("%Y-%m-%d %H:%M:%S")
    result_text = formatted_start_time + "\n"
    result_text = result_text + str(category)
    _LOGGER.debug(f"result_text: {result_text}")

    # get db connection
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Insert a new record of plate number
    _LOGGER.info(f"Storing plate number in database: {plate_number}")
    cursor.execute("""
        INSERT INTO plates (detection_time, score, plate_number, frigate_event, camera_name) VALUES (?, ?, ?, ?, ?)
    """, (formatted_start_time, score, plate_number, frigate_event, after_data['camera']))

    # set the sublabel
    set_sublabel(frigate_url, frigate_event, plate_number)

    # Commit the changes
    conn.commit()
    conn.close()


def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""    
        CREATE TABLE IF NOT EXISTS plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TIMESTAMP NOT NULL,
            score TEXT NOT NULL,
            plate_number TEXT NOT NULL,
            frigate_event TEXT NOT NULL UNIQUE,
            camera_name TEXT NOT NULL
        )    
    """)
    conn.commit()
    conn.close()

def load_config():
    global config
    with open(CONFIG_PATH, 'r') as config_file:
        config = yaml.safe_load(config_file)

def run_mqtt_client():
    _LOGGER.info(f"Starting MQTT client. Connecting to: {config['frigate']['mqtt_server']}")
    now = datetime.now()
    current_time = now.strftime("%Y%m%d%H%M%S")

    # setup mqtt client
    client = mqtt.Client("FrigatePlateRecognizer" + current_time)
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect

    # check if we are using authentication and set username/password if so
    if config['frigate']['mqtt_auth']:
        username = config['frigate']['mqtt_username']
        password = config['frigate']['mqtt_password']
        client.username_pw_set(username, password)

    client.connect(config['frigate']['mqtt_server'])
    client.loop_forever()

def load_logger():
    global _LOGGER
    _LOGGER = logging.getLogger(__name__)
    _LOGGER.setLevel(config['logger_level'])

    # Create a formatter to customize the log message format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Create a console handler and set the level to display all messages
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)

    # Create a file handler to log messages to a file
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Add the handlers to the logger
    _LOGGER.addHandler(console_handler)
    _LOGGER.addHandler(file_handler)
    

def main():
    load_config()
    setup_db()
    load_logger()

    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    _LOGGER.info(f"Time: {current_time}")
    _LOGGER.info(f"Python Version: {sys.version}")
    _LOGGER.info(f"Frigate Plate Recognizer Version: {VERSION}")
    _LOGGER.debug(f"config: {config}")

    # start mqtt client
    mqtt_process = multiprocessing.Process(target=run_mqtt_client)
    mqtt_process.start()
    mqtt_process.join()


if __name__ == '__main__':
    main()