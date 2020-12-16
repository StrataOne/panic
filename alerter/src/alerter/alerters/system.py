import copy
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Type, List

import pika.exceptions

from src.alerter.alerters.alerter import Alerter
from src.alerter.alerts.system_alerts import (
    InvalidUrlAlert, OpenFileDescriptorsIncreasedAboveThresholdAlert,
    SystemBackUpAgainAlert,
    SystemCPUUsageDecreasedBelowThresholdAlert,
    SystemCPUUsageIncreasedAboveThresholdAlert,
    SystemRAMUsageDecreasedBelowThresholdAlert,
    SystemRAMUsageIncreasedAboveThresholdAlert, SystemStillDownAlert,
    SystemStorageUsageDecreasedBelowThresholdAlert,
    SystemStorageUsageIncreasedAboveThresholdAlert, SystemWentDownAtAlert,
    OpenFileDescriptorsDecreasedBelowThresholdAlert, MetricNotFoundErrorAlert)
from src.configs.system_alerts import SystemAlertsConfig
from src.utils.alert import floaty
from src.utils.constants import ALERT_EXCHANGE, HEALTH_CHECK_EXCHANGE
from src.utils.exceptions import (MessageWasNotDeliveredException,
                                  ReceivedUnexpectedDataException)
from src.utils.timing import TimedTaskLimiter
from src.utils.types import IncreasedAboveThresholdSystemAlert, \
    DecreasedBelowThresholdSystemAlert, str_to_bool, \
    convert_to_int_if_not_none_and_not_empty_str, \
    convert_to_float_if_not_none_and_not_empty_str

OPEN_FD_LIMITER_NAME = 'open_file_descriptors'
CPU_USE_LIMITER_NAME = 'system_cpu_usage'
STORAGE_USE_LIMITER_NAME = 'system_storage_usage'
RAM_USE_LIMITER_NAME = 'system_ram_usage'
IS_DOWN_LIMITER_NAME = 'system_is_down'


class SystemAlerter(Alerter):
    def __init__(self, alerter_name: str,
                 system_alerts_config: SystemAlertsConfig,
                 logger: logging.Logger) -> None:
        super().__init__(alerter_name, logger)

        self._system_alerts_config = system_alerts_config
        self._queue_used = ''
        self._system_initial_downtime_alert_sent = {}
        self._system_critical_timed_task_limiters = {}

    @property
    def alerts_configs(self) -> SystemAlertsConfig:
        return self._system_alerts_config

    def _create_state_for_system(self, system_id: str) -> None:
        # Initialize initial downtime alert sent
        if system_id not in self._system_initial_downtime_alert_sent:
            self._system_initial_downtime_alert_sent[system_id] = False

        # Initialize timed task limiters
        if system_id not in self._system_critical_timed_task_limiters:
            open_fd = self.alerts_configs.open_file_descriptors
            cpu_use = self.alerts_configs.system_cpu_usage
            storage = self.alerts_configs.system_storage_usage
            ram_use = self.alerts_configs.system_ram_usage
            is_down = self.alerts_configs.system_is_down

            self._system_critical_timed_task_limiters[system_id] = {}
            system_critical_limiters = \
                self._system_critical_timed_task_limiters[system_id]

            system_critical_limiters[OPEN_FD_LIMITER_NAME] = TimedTaskLimiter(
                timedelta(seconds=int(open_fd['critical_repeat']))
            )
            system_critical_limiters[CPU_USE_LIMITER_NAME] = TimedTaskLimiter(
                timedelta(seconds=int(cpu_use['critical_repeat']))
            )
            system_critical_limiters[STORAGE_USE_LIMITER_NAME] = \
                TimedTaskLimiter(
                    timedelta(seconds=int(storage['critical_repeat']))
                )
            system_critical_limiters[RAM_USE_LIMITER_NAME] = TimedTaskLimiter(
                timedelta(seconds=int(ram_use['critical_repeat']))
            )
            system_critical_limiters[IS_DOWN_LIMITER_NAME] = TimedTaskLimiter(
                timedelta(seconds=int(is_down['critical_repeat']))
            )

    def _initialize_rabbitmq(self) -> None:
        # An alerter is both a consumer and producer, therefore we need to
        # initialize both the consuming and producing configurations.
        self.rabbitmq.connect_till_successful()

        # Set consuming configuration
        self.logger.info("Creating '{}' exchange".format(ALERT_EXCHANGE))
        self.rabbitmq.exchange_declare(exchange=ALERT_EXCHANGE,
                                       exchange_type='topic', passive=False,
                                       durable=True, auto_delete=False,
                                       internal=False)
        self._queue_used = "system_alerter_queue_" + \
                           self.alerts_configs.parent_id
        self.logger.info("Creating queue '{}'".format(self._queue_used))
        self.rabbitmq.queue_declare(self._queue_used, passive=False,
                                    durable=True, exclusive=False,
                                    auto_delete=False)
        routing_key = "alerter.system." + self.alerts_configs.parent_id
        self.logger.info("Binding queue '{}' to exchange 'alert' with routing "
                         "key '{}'".format(self._queue_used, routing_key))
        self.rabbitmq.queue_bind(queue=self._queue_used,
                                 exchange=ALERT_EXCHANGE,
                                 routing_key=routing_key)

        # Pre-fetch count is 5 times less the maximum queue size
        prefetch_count = round(self.publishing_queue.maxsize / 5)
        self.rabbitmq.basic_qos(prefetch_count=prefetch_count)
        self.logger.info("Declaring consuming intentions")
        self.rabbitmq.basic_consume(queue=self._queue_used,
                                    on_message_callback=self._process_data,
                                    auto_ack=False,
                                    exclusive=False,
                                    consumer_tag=None)

        # Set producing configuration
        self.logger.info("Setting delivery confirmation on RabbitMQ channel")
        self.rabbitmq.confirm_delivery()
        self.logger.info("Creating '{}' exchange".format(ALERT_EXCHANGE))
        self.rabbitmq.exchange_declare(ALERT_EXCHANGE, 'topic', False, True,
                                       False, False)
        self.logger.info("Creating '{}' exchange".format(HEALTH_CHECK_EXCHANGE))
        self.rabbitmq.exchange_declare(HEALTH_CHECK_EXCHANGE, 'topic', False,
                                       True, False, False)

    def _process_data(self,
                      ch: pika.adapters.blocking_connection.BlockingChannel,
                      method: pika.spec.Basic.Deliver,
                      properties: pika.spec.BasicProperties,
                      body: bytes) -> None:
        data_received = json.loads(body.decode())
        self.logger.info("Received {}. Now processing this data.".format(
            data_received))

        parsed_routing_key = method.routing_key.split('.')
        processing_error = False
        data_for_alerting = []
        try:
            if self.alerts_configs.parent_id in parsed_routing_key:
                if 'result' in data_received:
                    data = data_received['result']['data']
                    meta_data = data_received['result']['meta_data']
                    system_id = meta_data['system_id']
                    self._create_state_for_system(system_id)

                    self._process_results(data, meta_data, data_for_alerting)
                elif 'error' in data_received:
                    meta_data = data_received['error']['meta_data']
                    system_id = meta_data['system_id']
                    self._create_state_for_system(system_id)

                    self._process_errors(data_received['error'],
                                         data_for_alerting)
                else:
                    raise ReceivedUnexpectedDataException(
                        "{}: _process_data".format(self))
            else:
                raise ReceivedUnexpectedDataException(
                    "{}: _process_data".format(self))

            self.logger.info("Data processed successfully.")
        except Exception as e:
            self.logger.error("Error when processing {}".format(data_received))
            self.logger.exception(e)
            processing_error = True

        # If the data is processed, it can be acknowledged.
        self.rabbitmq.basic_ack(method.delivery_tag, False)

        # Place the data on the publishing queue if there were no processing
        # errors. This is done after acknowledging the data, so that if
        # acknowledgement fails, the data is processed again and we do not have
        # duplication of data in the queue.
        if not processing_error:
            self._place_latest_data_on_queue(data_for_alerting)

        # Send any data waiting in the publisher queue, if any
        try:
            self._send_data()

            if not processing_error:
                heartbeat = {
                    'component_name': self.alerter_name,
                    'timestamp': datetime.now().timestamp()
                }
                self._send_heartbeat(heartbeat)
        except MessageWasNotDeliveredException as e:
            # Log the message and do not raise the exception so that the
            # message can be acknowledged and removed from the rabbit queue.
            # Note this message will still reside in the publisher queue.
            self.logger.exception(e)
        except Exception as e:
            # For any other exception acknowledge and raise it, so the
            # message is removed from the rabbit queue as this message will now
            # reside in the publisher queue
            raise e

    def _process_errors(self, error_data: Dict,
                        data_for_alerting: List) -> None:
        is_down = self.alerts_configs.system_is_down
        meta_data = error_data['meta_data']
        data = error_data['data']
        if int(error_data['code']) == 5003:
            alert = MetricNotFoundErrorAlert(
                error_data['message'], 'ERROR', meta_data['time'],
                meta_data['system_parent_id'], meta_data['system_id']
            )
            data_for_alerting.append(alert.alert_data)
            self.logger.debug('Successfully classified alert {}'
                              ''.format(alert.alert_data))
        elif int(error_data['code']) == 5009:
            alert = InvalidUrlAlert(
                error_data['message'], 'ERROR', meta_data['time'],
                meta_data['system_parent_id'], meta_data['system_id']
            )
            data_for_alerting.append(alert.alert_data)
            self.logger.debug("Successfully classified alert {}"
                              "".format(alert.alert_data))
        elif int(error_data['code']) == 5004:
            if str_to_bool(is_down['enabled']):
                current = float(data['went_down_at']['current'])
                monitoring_timestamp = float(meta_data['time'])
                monitoring_datetime = datetime.fromtimestamp(
                    monitoring_timestamp)
                critical_limiters = self._system_critical_timed_task_limiters[
                    meta_data['system_id']]
                is_down_critical_limiter = critical_limiters[
                    IS_DOWN_LIMITER_NAME]
                downtime = monitoring_timestamp - current

                critical_threshold = \
                    convert_to_int_if_not_none_and_not_empty_str(
                        is_down['critical_threshold'], None)
                critical_enabled = str_to_bool(is_down['critical_enabled'])
                warning_threshold = \
                    convert_to_int_if_not_none_and_not_empty_str(
                        is_down['warning_threshold'], None)
                warning_enabled = str_to_bool(is_down['warning_enabled'])

                if not \
                        self._system_initial_downtime_alert_sent[
                            meta_data['system_id']]:
                    if critical_enabled and critical_threshold <= downtime:
                        alert = SystemWentDownAtAlert(
                            meta_data['system_name'], 'CRITICAL',
                            meta_data['time'], meta_data['system_parent_id'],
                            meta_data['system_id']
                        )
                        data_for_alerting.append(alert.alert_data)
                        self.logger.debug("Successfully classified alert {}"
                                          "".format(alert.alert_data))
                        is_down_critical_limiter.set_last_time_that_did_task(
                            monitoring_datetime)
                        self._system_initial_downtime_alert_sent[
                            meta_data['system_id']] = True
                    elif warning_enabled and warning_threshold <= downtime:
                        alert = SystemWentDownAtAlert(
                            meta_data['system_name'], 'WARNING',
                            meta_data['time'], meta_data['system_parent_id'],
                            meta_data['system_id']
                        )
                        data_for_alerting.append(alert.alert_data)
                        self.logger.debug("Successfully classified alert {}"
                                          "".format(alert.alert_data))
                        is_down_critical_limiter.set_last_time_that_did_task(
                            monitoring_datetime)
                        self._system_initial_downtime_alert_sent[
                            meta_data['system_id']] = True
                else:
                    if critical_enabled and \
                            is_down_critical_limiter.can_do_task(
                                monitoring_datetime):
                        alert = SystemStillDownAlert(
                            meta_data['system_name'], downtime, 'CRITICAL',
                            meta_data['time'], meta_data['system_parent_id'],
                            meta_data['system_id']
                        )
                        data_for_alerting.append(alert.alert_data)
                        self.logger.debug("Successfully classified alert {}"
                                          "".format(alert.alert_data))
                        is_down_critical_limiter.set_last_time_that_did_task(
                            monitoring_datetime)

    def _process_results(self, metrics: Dict, meta_data: Dict,
                         data_for_alerting: List) -> None:
        open_fd = self.alerts_configs.open_file_descriptors
        cpu_use = self.alerts_configs.system_cpu_usage
        storage = self.alerts_configs.system_storage_usage
        ram_use = self.alerts_configs.system_ram_usage
        is_down = self.alerts_configs.system_is_down

        if str_to_bool(is_down['enabled']):
            previous = metrics['went_down_at']['previous']
            critical_limiters = self._system_critical_timed_task_limiters[
                meta_data['system_id']]
            is_down_critical_limiter = critical_limiters[IS_DOWN_LIMITER_NAME]

            if previous is not None:
                alert = SystemBackUpAgainAlert(
                    meta_data['system_name'], 'INFO',
                    meta_data['last_monitored'], meta_data['system_parent_id'],
                    meta_data['system_id']
                )
                data_for_alerting.append(alert.alert_data)
                self.logger.debug("Successfully classified alert {}"
                                  "".format(alert.alert_data))
                self._system_initial_downtime_alert_sent[
                    meta_data['system_id']] = False
                is_down_critical_limiter.reset()

        if str_to_bool(open_fd['enabled']):
            current = metrics['open_file_descriptors']['current']
            previous = metrics['open_file_descriptors']['previous']
            if current not in [previous, None]:
                self._classify_alert(
                    current, floaty(previous), open_fd, meta_data,
                    OpenFileDescriptorsIncreasedAboveThresholdAlert,
                    OpenFileDescriptorsDecreasedBelowThresholdAlert,
                    data_for_alerting, OPEN_FD_LIMITER_NAME
                )
        if str_to_bool(storage['enabled']):
            current = metrics['system_storage_usage']['current']
            previous = metrics['system_storage_usage']['previous']
            if current not in [previous, None]:
                self._classify_alert(
                    current, floaty(previous), storage, meta_data,
                    SystemStorageUsageIncreasedAboveThresholdAlert,
                    SystemStorageUsageDecreasedBelowThresholdAlert,
                    data_for_alerting, STORAGE_USE_LIMITER_NAME
                )
        if str_to_bool(cpu_use['enabled']):
            current = metrics['system_cpu_usage']['current']
            previous = metrics['system_cpu_usage']['previous']
            if current not in [previous, None]:
                self._classify_alert(
                    current, floaty(previous), cpu_use, meta_data,
                    SystemCPUUsageIncreasedAboveThresholdAlert,
                    SystemCPUUsageDecreasedBelowThresholdAlert,
                    data_for_alerting, CPU_USE_LIMITER_NAME
                )
        if str_to_bool(ram_use['enabled']):
            current = metrics['system_ram_usage']['current']
            previous = metrics['system_ram_usage']['previous']
            if current not in [previous, None]:
                self._classify_alert(
                    current, floaty(previous), cpu_use, meta_data,
                    SystemRAMUsageIncreasedAboveThresholdAlert,
                    SystemRAMUsageDecreasedBelowThresholdAlert,
                    data_for_alerting, RAM_USE_LIMITER_NAME
                )

    def _classify_alert(
            self, current: float, previous: float, config: Dict,
            meta_data: Dict, increased_above_threshold_alert:
            Type[IncreasedAboveThresholdSystemAlert],
            decreased_below_threshold_alert:
            Type[DecreasedBelowThresholdSystemAlert], data_for_alerting: List,
            critical_limiter_name: str
    ) -> None:
        warning_threshold = convert_to_float_if_not_none_and_not_empty_str(
            config['warning_threshold'], None)
        critical_threshold = convert_to_float_if_not_none_and_not_empty_str(
            config['critical_threshold'], None)
        warning_enabled = str_to_bool(config['warning_enabled'])
        critical_enabled = str_to_bool(config['critical_enabled'])
        critical_limiters = self._system_critical_timed_task_limiters[
            meta_data['system_id']]
        critical_limiter = critical_limiters[critical_limiter_name]

        if warning_enabled:
            if (warning_threshold <= current < critical_threshold) and not \
                    (warning_threshold <= previous):
                alert = \
                    increased_above_threshold_alert(
                        meta_data['system_name'], current, 'WARNING',
                        meta_data['last_monitored'], 'WARNING',
                        meta_data['system_parent_id'],
                        meta_data['system_id']
                    )
                data_for_alerting.append(alert.alert_data)
                self.logger.debug("Successfully classified alert {}"
                                  "".format(alert.alert_data))
            elif current < warning_threshold <= previous:
                alert = \
                    decreased_below_threshold_alert(
                        meta_data['system_name'], current, 'INFO',
                        meta_data['last_monitored'], 'WARNING',
                        meta_data['system_parent_id'],
                        meta_data['system_id']
                    )
                data_for_alerting.append(alert.alert_data)
                self.logger.debug("Successfully classified alert {}"
                                  "".format(alert.alert_data))

        if critical_enabled:
            monitoring_datetime = datetime.fromtimestamp(
                float(meta_data['last_monitored']))
            if current >= critical_threshold and \
                    critical_limiter.can_do_task(monitoring_datetime):
                alert = \
                    increased_above_threshold_alert(
                        meta_data['system_name'], current, 'CRITICAL',
                        meta_data['last_monitored'], 'CRITICAL',
                        meta_data['system_parent_id'],
                        meta_data['system_id']
                    )
                data_for_alerting.append(alert.alert_data)
                self.logger.debug("Successfully classified alert {}"
                                  "".format(alert.alert_data))
                critical_limiter.set_last_time_that_did_task(
                    monitoring_datetime)
            elif warning_threshold < current < critical_threshold <= previous:
                alert = \
                    decreased_below_threshold_alert(
                        meta_data['system_name'], current, 'INFO',
                        meta_data['last_monitored'], 'CRITICAL',
                        meta_data['system_parent_id'],
                        meta_data['system_id']
                    )
                data_for_alerting.append(alert.alert_data)
                self.logger.debug("Successfully classified alert {}"
                                  "".format(alert.alert_data))
                critical_limiter.reset()

    def _place_latest_data_on_queue(self, data_list: List) -> None:
        # Place the latest alert data on the publishing queue. If the
        # queue is full, remove old data.
        for alert in data_list:
            self.logger.debug("Adding {} to the publishing queue.".format(
                alert))
            if self.publishing_queue.full():
                self.publishing_queue.get()
            self.publishing_queue.put({
                'exchange': ALERT_EXCHANGE,
                'routing_key': 'alert_router.system',
                'data': copy.deepcopy(alert)})
            self.logger.debug("{} added to the publishing queue "
                              "successfully.".format(alert))
