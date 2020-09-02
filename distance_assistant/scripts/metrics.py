from abc import ABCMeta, abstractmethod
import datetime
from dateutil.tz.tz import tzutc
import json
import os
import time

import boto3

from darknet_custom import Color
from distance_assistant.msg import MetricsMsg


class AbstractMetricsPublisher:
    __metaclass__ = ABCMeta

    @abstractmethod
    def __init__(self, location):
        self.location = location

    @staticmethod
    def _get_record_from_message(msg):
        json_record = {
            'e': msg.epoch,
            'l': msg.location,
            'nc': msg.noncompliant_detections,
            't': msg.total_detections
        }

        return bytes(json.dumps(json_record).encode('utf-8'))

    @abstractmethod
    def publish(self, msg):
        raise NotImplementedError('Method not implemented in abstract class.')


class KinesisMetricsPublisher(AbstractMetricsPublisher):

    def __init__(self, location=None, aws_region_name=None,
                 identity_pool_id=None, kinesis_stream=None,
                 **kwargs):

        super(KinesisMetricsPublisher, self).__init__(location)

        self.aws_region_name = aws_region_name
        self.identity_pool_id = identity_pool_id
        self.kinesis_stream = kinesis_stream

        self.credentials = None

        self.cognito_identity = boto3.client(
            'cognito-identity',
            region_name=aws_region_name)

    def _credentials_expired(self):
        now = datetime.datetime.now(tzutc())
        return self.credentials['Expiration'] >= now\
            if self.credentials is not None else True

    def _get_kinesis_client(self):
        """ Obtains temporary AWS credentials and instantiates an
            Amazon Kinesis Firehose client.
        """
        if self.credentials is None or not self._credentials_expired():
            id = self.cognito_identity.get_id(IdentityPoolId=self.identity_pool_id)
            self.credentials = self.cognito_identity.get_credentials_for_identity(
                IdentityId=id['IdentityId']
            )['Credentials']

            self.kinesis_client = boto3.client(
                'firehose',
                aws_access_key_id = self.credentials['AccessKeyId'],
                aws_secret_access_key = self.credentials['SecretKey'],
                aws_session_token = self.credentials['SessionToken'],
                region_name=self.aws_region_name
            )

    def publish(self, msg):
        self._get_kinesis_client()
        self.kinesis_client.put_record(
            DeliveryStreamName = self.kinesis_stream,
            Record = {
                'Data': self._get_record_from_message(msg)
            }
        )
    

class LocalMetricsPublisher(AbstractMetricsPublisher):

    def __init__(self, location=None, metrics_path=None,
                 max_file_size=262144, **kwargs):

        super(LocalMetricsPublisher, self).__init__(location)

        self.bytes_written = 0
        self.log_file = None
        self.metrics_path = metrics_path
        self.max_file_size = max_file_size
        self.stream = None

    def _get_stream(self):

        if self.bytes_written >= self.max_file_size:
            self.stream.close()
            self.stream = None

        if self.stream is None:
            filename = datetime.datetime.utcnow().strftime(
                '%Y%m%d-%H%M%S'
            )
            filename = os.path.join(self.metrics_path, filename)
            self.log_file = filename
            self.stream = open(filename, 'w', 0)
            self.bytes_written = 0

    def __del__(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def publish(self, msg):
        self._get_stream()
        size_before = self.stream.tell()
        self.stream.write(self._get_record_from_message(msg))
        self.bytes_written += (self.stream.tell() - size_before)


class RosMetricsPublisher(AbstractMetricsPublisher):

    def __init__(self, location=None, ros_publisher=None, **kwargs):
        super(RosMetricsPublisher, self).__init__(location)

        self.ros_publisher = ros_publisher

    def publish(self, msg):
        self.ros_publisher.publish(msg)


AbstractMetricsPublisher.register(KinesisMetricsPublisher)
AbstractMetricsPublisher.register(LocalMetricsPublisher)
AbstractMetricsPublisher.register(RosMetricsPublisher)


class Metrics(object):

    def __init__(self, location='not_configured', sampling_period=60, **kwargs):

        self.location = location
        self.sampling_period = sampling_period
        self.last_time_sampled = int(time.time())
        self.publishers = []

    def register_publisher(self, publisher):
        if not isinstance(publisher, AbstractMetricsPublisher):
            raise ValueError('Publisher not of the AbstractMetricsPublisher type.')

        self.publishers.append(publisher)

    def compute_metrics(self, detections):
        """
        Computes metrics for a single frame.

        Arguments:
            detections: list of people detections ([Detection])
        Returns:
            None
        """

        current_time = int(time.time())

        # Only sample if enough time has elapsed between samples.
        if current_time - self.last_time_sampled >= self.sampling_period:
            self.last_time_sampled = current_time

            msg = MetricsMsg()

            msg.epoch = int(current_time)
            msg.location = self.location
            msg.noncompliant_detections =\
                sum([detection.color == Color.RED for detection in detections])
            msg.total_detections = len(detections)

            for publisher in self.publishers:
                publisher.publish(msg)
