import datetime

import redis
import redis_lock
from huey.consumer import Consumer, Scheduler, EVENT_CHECKING_PERIODIC, EVENT_SCHEDULING_PERIODIC


class HueyxScheduler(Scheduler):
    def __init__(self, *args, **kwargs):
        self.multiple_scheduler_locking = kwargs.pop('multiple_scheduler_locking', False)
        super().__init__(*args, **kwargs)

    """
    Extend the usual Scheduler with the ability to prevent multiple periodic task execution due to multiple
    huey worker and finally multiple running Schedulers.
    This is done by locking the specific execution time pattern on redis.
    """

    def enqueue_periodic_tasks(self, now, start):
        self.huey.emit_status(
            EVENT_CHECKING_PERIODIC,
            timestamp=self.get_timestamp())
        self._logger.debug('Checking periodic tasks')
        for task in self.huey.read_periodic(now):
            if self.check_and_set_for_multiple_execution(task, now):
                self.enqueue_periodic_task(task)
        self.restart_dead_tasks()
        return True

    def check_and_set_for_multiple_execution(self, task, now):
        """
        Checks if the task already has been scheduled from another huey worker and if not, marks the task as scheduled.
        :param task:
        :param now:
        :return: Bool if task can be scheduled.
        """

        if not self.multiple_scheduler_locking:
            return True

        conn: redis.ConnectionPool = self.huey.storage.conn
        full_name = f"huey.{self.huey.name}.{task.name}"
        lock_name = full_name + ".periodic_lock"
        pattern_name = full_name + '.time_pattern'
        with redis_lock.Lock(conn, lock_name, expire=60):
            if not self._can_execute(pattern_name, now):
                self._logger.info(
                    f'{full_name}: Do not schedule periodic task because this time pattern has already been scheduled.'
                )
                return False
            else:
                self._set_execution_time_pattern(pattern_name, now)
                self._logger.info(
                    f'{full_name}: Set time pattern for periodic task execution.'
                )
                return True

    def enqueue_periodic_task(self, task):
        self.huey.emit_task(
            EVENT_SCHEDULING_PERIODIC,
            task,
            timestamp=self.get_timestamp())
        self._logger.info('Scheduling periodic task %s.', task)
        self.enqueue(task)

    def _create_time_pattern(self, now: datetime.datetime):
        """Standardized time pattern which the task is executed."""
        _, month, day, hour, minute, _, week_day, _, _ = now.timetuple()
        current_time_pattern = f"month{month}.day{day}.week_day{week_day}.hour{hour}.minute{minute}"
        return current_time_pattern

    def _can_execute(self, full_name: str, now: datetime.datetime):
        """Check if the current time pattern already has been executed."""
        current_time_pattern = self._create_time_pattern(now)

        last_time_pattern = self._get_last_execution_time_pattern(full_name)
        return last_time_pattern != current_time_pattern

    def _get_last_execution_time_pattern(self, full_name: str):
        conn = self.huey.storage.conn
        current_val = conn.get(full_name)
        if current_val is not None:
            current_val = current_val.decode("utf-8")
        return current_val

    def _set_execution_time_pattern(self, full_name: str, now):
        current_time_pattern = self._create_time_pattern(now)
        conn: redis.Redis = self.huey.storage.conn
        conn.set(full_name, current_time_pattern)

    def restart_dead_tasks(self):
        self._logger.debug('Restart dead tasks')
        for task in self.huey.get_dead_tasks():
            for task_type in self.huey.restartable_tasks:   # TODO: exception if not found
                if task_type.task_class.__name__ in task.name:
                    self.huey.revoke_by_id(task.id)
                    self.huey.get(self.huey.get_heartbeat_observation_key(task.id))
                    self._logger.debug(f'Restart {task_type.task_class.__name__}({str(task.args), str(task.kwargs)})')
                    task_type(*task.args, **task.kwargs)
                    break


class HueyxConsumer(Consumer):
    def __init__(self, *args, **kwargs):
        self.multiple_scheduler_locking = kwargs.pop('multiple_scheduler_locking', False)
        super().__init__(*args, **kwargs)

    def _create_scheduler(self):
        self._logger.info('multiple_scheduler_locking: ' + str(self.multiple_scheduler_locking))
        return HueyxScheduler(
            huey=self.huey,
            interval=self.scheduler_interval,
            utc=self.utc,
            periodic=self.periodic,
            multiple_scheduler_locking=self.multiple_scheduler_locking)
