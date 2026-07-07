"""
Integration Test용 실제 서비스 연결 Factory.

⚠️  이 모듈은 Integration Test용입니다.
    실제 Docker 서비스(Redis, PostgreSQL, Celery)와 연결합니다.
    docker-compose up -d 로 서비스가 실행 중이어야 합니다.

⚠️  전역 tests 폴더는 통합 테스트 전용입니다.
    baldur Unit Test는 별도 repo에서 진행: https://github.com/baldurhq/baldur

Usage:
    from tests.factories.integration import (
        RealRedisClientFactory,
        RealDatabaseFactory,
        CeleryTaskRunner,
    )

    # 실제 Redis 연결
    redis = RealRedisClientFactory.create()
    redis.set("key", "value")

    # Celery 태스크 실행 (실제 워커 필요)
    runner = CeleryTaskRunner()
    result = runner.run_and_wait("shopping.tasks.payment_tasks.finalize_payment_confirm", args=[...])
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tests.factories.constants import (
    DatabaseTestConfig,
    RedisTestConfig,
)


class RealRedisClientFactory:
    """
    실제 Redis 클라이언트 Factory.

    Docker Compose의 Redis에 연결합니다.
    """

    @staticmethod
    def create(
        host: str | None = None,
        port: int | None = None,
        db: int | None = None,
        decode_responses: bool = True,
    ):
        """
        실제 Redis 클라이언트 생성.

        Args:
            host: Redis 호스트 (기본: localhost)
            port: Redis 포트 (기본: 6379)
            db: Redis DB 번호 (기본: 15 for test)
            decode_responses: 응답 디코딩 여부

        Returns:
            redis.Redis 클라이언트

        Raises:
            ConnectionError: Redis 연결 실패 시
        """
        import redis

        config = RedisTestConfig()

        client = redis.Redis(
            host=host or os.environ.get("REDIS_HOST", config.DEFAULT_HOST),
            port=port or int(os.environ.get("REDIS_PORT", config.DEFAULT_PORT)),
            db=db if db is not None else config.TEST_DB,
            decode_responses=decode_responses,
        )

        # 연결 확인
        client.ping()
        return client

    @staticmethod
    def create_with_cleanup(
        prefix: str = "test:",
        **kwargs,
    ):
        """
        테스트 후 자동 정리되는 Redis 클라이언트 생성.

        Args:
            prefix: 키 프리픽스 (정리 대상)
            **kwargs: create() 인자

        Returns:
            (client, cleanup_func) 튜플
        """
        client = RealRedisClientFactory.create(**kwargs)

        def cleanup():
            for key in client.keys(f"{prefix}*"):
                client.delete(key)

        return client, cleanup

    @staticmethod
    def create_for_circuit_breaker():
        """CB 테스트용 Redis 클라이언트."""
        config = RedisTestConfig()
        return RealRedisClientFactory.create_with_cleanup(
            prefix=config.CB_KEY_PREFIX,
        )

    @staticmethod
    def create_for_dlq():
        """DLQ 테스트용 Redis 클라이언트."""
        config = RedisTestConfig()
        return RealRedisClientFactory.create_with_cleanup(
            prefix=config.DLQ_KEY_PREFIX,
        )


class RealDatabaseFactory:
    """
    실제 PostgreSQL 연결 Factory.

    Docker Compose의 PostgreSQL에 연결합니다.
    """

    @staticmethod
    def create_connection():
        """
        PostgreSQL 연결 생성.

        Returns:
            psycopg2 connection 객체

        Raises:
            OperationalError: DB 연결 실패 시
        """
        import psycopg2

        config = DatabaseTestConfig()

        return psycopg2.connect(
            host=os.environ.get("DB_HOST", config.DEFAULT_HOST),
            port=int(os.environ.get("DB_PORT", config.DEFAULT_PORT)),
            database=os.environ.get("DB_NAME", config.DEFAULT_DB),
            user=os.environ.get("DB_USER", config.DEFAULT_USER),
            password=os.environ.get("DB_PASSWORD", config.DEFAULT_PASSWORD),
        )

    @staticmethod
    def create_django_connection():
        """
        Django ORM 연결 설정.

        Django가 설정된 환경에서 호출해야 합니다.

        Returns:
            Django database connection
        """
        from django.db import connection

        return connection

    @staticmethod
    def execute_sql(sql: str, params: tuple | None = None) -> list[tuple]:
        """
        SQL 직접 실행 (테스트 데이터 설정/검증용).

        Args:
            sql: SQL 쿼리
            params: 쿼리 파라미터

        Returns:
            결과 행 리스트
        """
        conn = RealDatabaseFactory.create_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                if cursor.description:
                    rows: list[tuple] = cursor.fetchall()
                    return rows
                conn.commit()
                return []
        finally:
            conn.close()


class CeleryTaskRunner:
    """
    Celery 태스크 실행기.

    실제 Celery 워커와 연동하여 태스크를 실행하고 결과를 기다립니다.
    docker-compose의 celery_worker가 실행 중이어야 합니다.
    """

    def __init__(self, timeout: int = 30, poll_interval: float = 0.5):
        """
        Args:
            timeout: 최대 대기 시간 (초)
            poll_interval: 결과 폴링 간격 (초)
        """
        self.timeout = timeout
        self.poll_interval = poll_interval

    def run_and_wait(
        self,
        task_name: str,
        args: tuple | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        """
        태스크를 실행하고 결과를 기다림.

        Args:
            task_name: 태스크 전체 경로 (e.g., "shopping.tasks.payment_tasks.finalize_payment_confirm")
            args: 위치 인자
            kwargs: 키워드 인자

        Returns:
            태스크 결과

        Raises:
            TimeoutError: 타임아웃 시
            Exception: 태스크 실패 시
        """
        from celery import current_app

        task = current_app.send_task(
            task_name,
            args=args or (),
            kwargs=kwargs or {},
        )

        start_time = time.time()
        while not task.ready():
            if time.time() - start_time > self.timeout:
                raise TimeoutError(f"Task {task_name} timed out after {self.timeout}s")
            time.sleep(self.poll_interval)

        if task.failed():
            raise task.result  # Re-raise the exception

        return task.result

    def run_async(
        self,
        task_name: str,
        args: tuple | None = None,
        kwargs: dict | None = None,
        countdown: int | None = None,
    ):
        """
        태스크를 비동기로 실행 (결과 대기 안 함).

        Args:
            task_name: 태스크 전체 경로
            args: 위치 인자
            kwargs: 키워드 인자
            countdown: 실행 지연 시간 (초)

        Returns:
            AsyncResult 객체
        """
        from celery import current_app

        return current_app.send_task(
            task_name,
            args=args or (),
            kwargs=kwargs or {},
            countdown=countdown,
        )

    def run_chain(self, *tasks) -> Any:
        """
        태스크 체인 실행.

        Args:
            *tasks: (task_name, args, kwargs) 튜플 리스트

        Returns:
            마지막 태스크 결과
        """
        from celery import chain, current_app

        signatures = []
        for task_info in tasks:
            if isinstance(task_info, str):
                signatures.append(current_app.signature(task_info))
            else:
                name, args, kwargs = (
                    task_info[0],
                    task_info[1] if len(task_info) > 1 else (),
                    task_info[2] if len(task_info) > 2 else {},
                )
                signatures.append(current_app.signature(name, args=args, kwargs=kwargs))

        result = chain(*signatures).apply_async()
        return self._wait_for_result(result)

    def _wait_for_result(self, result) -> Any:
        """결과 대기 헬퍼."""
        start_time = time.time()
        while not result.ready():
            if time.time() - start_time > self.timeout:
                raise TimeoutError(f"Task chain timed out after {self.timeout}s")
            time.sleep(self.poll_interval)

        if result.failed():
            raise result.result

        return result.result


@dataclass
class IntegrationTestContext:
    """
    Integration Test 컨텍스트.

    Redis, DB, Celery 등 통합 테스트에 필요한 리소스를 관리합니다.
    """

    redis_client: Any = None
    db_connection: Any = None
    celery_runner: CeleryTaskRunner | None = None
    cleanup_funcs: list[Callable] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        use_redis: bool = True,
        use_db: bool = False,
        use_celery: bool = False,
    ) -> "IntegrationTestContext":
        """
        통합 테스트 컨텍스트 생성.

        Args:
            use_redis: Redis 연결 여부
            use_db: DB 연결 여부
            use_celery: Celery 러너 생성 여부

        Returns:
            IntegrationTestContext 인스턴스
        """
        ctx = cls()

        if use_redis:
            ctx.redis_client, cleanup = RealRedisClientFactory.create_with_cleanup()
            ctx.cleanup_funcs.append(cleanup)

        if use_db:
            ctx.db_connection = RealDatabaseFactory.create_connection()
            ctx.cleanup_funcs.append(lambda: ctx.db_connection.close())

        if use_celery:
            ctx.celery_runner = CeleryTaskRunner()

        return ctx

    def cleanup(self):
        """모든 리소스 정리."""
        for func in self.cleanup_funcs:
            try:
                func()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()


# Convenience functions


def with_real_redis(func):
    """
    실제 Redis를 사용하는 테스트 데코레이터.

    Usage:
        @with_real_redis
        def test_something(redis_client):
            redis_client.set("key", "value")
    """
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        client, cleanup = RealRedisClientFactory.create_with_cleanup()
        try:
            return func(client, *args, **kwargs)
        finally:
            cleanup()

    return wrapper


def with_integration_context(
    use_redis: bool = True,
    use_db: bool = False,
    use_celery: bool = False,
):
    """
    통합 테스트 컨텍스트 데코레이터.

    Usage:
        @with_integration_context(use_redis=True, use_celery=True)
        def test_something(ctx):
            ctx.redis_client.set("key", "value")
            result = ctx.celery_runner.run_and_wait("some.task", args=[...])
    """

    def decorator(func):
        from functools import wraps

        @wraps(func)
        def wrapper(*args, **kwargs):
            with IntegrationTestContext.create(
                use_redis=use_redis,
                use_db=use_db,
                use_celery=use_celery,
            ) as ctx:
                return func(ctx, *args, **kwargs)

        return wrapper

    return decorator
