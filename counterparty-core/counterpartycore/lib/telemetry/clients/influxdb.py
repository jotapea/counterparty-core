import os

import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS

from .interface import TelemetryClientI


class TelemetryClientInfluxDB(TelemetryClientI):
    def __init__(self):
        self.__influxdb_client = influxdb_client.InfluxDBClient(
            url=os.environ["INFLUXDB_URL"],
            token=os.environ["INFLUXDB_TOKEN"],
            org="cp",
        )

        self.__write_api = self.__influxdb_client.write_api(write_options=SYNCHRONOUS)

    def send(self, data):
        assert data["__influxdb"]

        tags = data["__influxdb"]["tags"]
        fields = data["__influxdb"]["fields"]

        point = influxdb_client.Point("node-heartbeat")

        for tag in tags:
            point.tag(tag, data[tag])

        for field in fields:
            point.field(field, data[field])

        self.__write_api.write(bucket="cp-nodes", org="cp", record=point)
