import datetime as dt
import json
from dataclasses import dataclass
from string import Template
import tempfile

from django.conf import settings
import snowflake.connector
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from posthog.batch_exports.service import SnowflakeBatchExportInputs

from posthog.temporal.workflows.base import (
    CreateBatchExportRunInputs,
    PostHogWorkflow,
    UpdateBatchExportRunStatusInputs,
    create_export_run,
    update_export_run_status,
)
from posthog.temporal.workflows.batch_exports import get_results_iterator, get_rows_count
from posthog.temporal.workflows.clickhouse import get_client


SELECT_QUERY_TEMPLATE = Template(
    """
    SELECT $fields
    FROM events
    WHERE
        timestamp >= toDateTime({data_interval_start}, 'UTC')
        AND timestamp < toDateTime({data_interval_end}, 'UTC')
        AND team_id = {team_id}
    """
)


@dataclass
class SnowflakeInsertInputs:
    """Inputs for Snowflake."""

    # TODO: do _not_ store credentials in temporal inputs. It makes it very hard
    # to keep track of where credentials are being stored and increases the
    # attach surface for credential leaks.

    team_id: int
    user: str
    password: str
    account: str
    database: str
    warehouse: str
    schema: str
    table_name: str
    data_interval_start: str
    data_interval_end: str


@activity.defn
async def insert_into_snowflake_activity(inputs: SnowflakeInsertInputs):
    """
    Activity streams data from ClickHouse to Snowflake.

    TODO: We're using JSON here, it's not the most efficient way to do this.

    TODO: at the moment this doesn't do anything about catching data that might
    be late being ingested into the specified time range. To work around this,
    as a little bit of a hack we should export data only up to an hour ago with
    the assumption that that will give it enough time to settle. I is a little
    tricky with the existing setup to properly partition the data into data we
    have or haven't processed yet. We have `_timestamp` in the events table, but
    this is the time
    """
    activity.logger.info("Running Snowflake export batch %s - %s", inputs.data_interval_start, inputs.data_interval_end)

    async with get_client() as client:
        if not await client.is_alive():
            raise ConnectionError("Cannot establish connection to ClickHouse")

        count = await get_rows_count(
            client=client,
            team_id=inputs.team_id,
            interval_start=inputs.data_interval_start,
            interval_end=inputs.data_interval_end,
        )

        if count == 0:
            activity.logger.info(
                "Nothing to export in batch %s - %s. Exiting.",
                inputs.data_interval_start,
                inputs.data_interval_end,
            )
            return

        activity.logger.info("BatchExporting %s rows to S3", count)

        conn = snowflake.connector.connect(
            user=inputs.user,
            password=inputs.password,
            account=inputs.account,
            warehouse=inputs.warehouse,
            database=inputs.database,
            schema=inputs.schema,
        )

        try:
            cursor = conn.cursor()
            cursor.execute(f'USE DATABASE "{inputs.database}"')
            cursor.execute(f'USE SCHEMA "{inputs.schema}"')

            # Create the table if it doesn't exist. Note that we use the same schema
            # as the snowflake-plugin for backwards compatibility.
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{inputs.database}"."{inputs.schema}"."{inputs.table_name}" (
                    "uuid" STRING,
                    "event" STRING,
                    "properties" VARIANT,
                    "elements" VARIANT,
                    "people_set" VARIANT,
                    "people_set_once" VARIANT,
                    "distinct_id" STRING,
                    "team_id" INTEGER,
                    "ip" STRING,
                    "site_url" STRING,
                    "timestamp" TIMESTAMP
                )
                COMMENT = 'PostHog generated events table'
                """
            )

            results_iterator = get_results_iterator(
                client=client,
                team_id=inputs.team_id,
                interval_start=inputs.data_interval_start,
                interval_end=inputs.data_interval_end,
            )

            local_results_file = tempfile.NamedTemporaryFile()
            try:
                while True:
                    try:
                        result = await results_iterator.__anext__()
                    except StopAsyncIteration:
                        break

                    if not result:
                        break

                    # Make sure the properties attribute is parsed as JSON
                    # TODO: this may well be a change that is worth making in
                    # the other batch export workflows as well, although I leave
                    # this as a followup.
                    row = {
                        **result,
                        "properties": json.loads(result.get("properties", "{}")),
                    }

                    # Write the results to a local file
                    local_results_file.write(json.dumps(row).encode("utf-8"))
                    local_results_file.write("\n".encode("utf-8"))

                    # Write results to Snowflake when the file reaches 50MB and
                    # reset the file, or if there is nothing else to write.
                    if (
                        local_results_file.tell()
                        and local_results_file.tell() > settings.BATCH_EXPORT_SNOWFLAKE_UPLOAD_CHUNK_SIZE_BYTES
                    ):
                        activity.logger.info("Uploading to Snowflake")

                        # Flush the file to make sure everything is written
                        local_results_file.flush()
                        cursor.execute(
                            f"""
                            PUT file://{local_results_file.name} @%"{inputs.table_name}"
                            """
                        )

                        # Delete the temporary file and create a new one
                        local_results_file.close()
                        local_results_file = tempfile.NamedTemporaryFile()

                # Flush the file to make sure everything is written
                local_results_file.flush()
                cursor.execute(
                    f"""
                    PUT file://{local_results_file.name} @%"{inputs.table_name}"
                    """
                )

                # We don't need the file anymore, close (and delete) it.
                local_results_file.close()

                cursor.execute(
                    f"""
                    COPY INTO "{inputs.table_name}"
                    FILE_FORMAT = (TYPE = 'JSON')
                    MATCH_BY_COLUMN_NAME = CASE_SENSITIVE
                    PURGE = TRUE
                    """
                )
            finally:
                local_results_file.close()
        finally:
            conn.close()


@workflow.defn(name="snowflake-export")
class SnowflakeBatchExportWorkflow(PostHogWorkflow):
    """A Temporal Workflow to export ClickHouse data into Snowflake.

    This Workflow is intended to be executed both manually and by a Temporal
    Schedule. When ran by a schedule, `data_interval_end` should be set to
    `None` so that we will fetch the end of the interval from the Temporal
    search attribute `TemporalScheduledStartTime`.
    """

    @staticmethod
    def parse_inputs(inputs: list[str]) -> SnowflakeBatchExportInputs:
        """Parse inputs from the management command CLI."""
        loaded = json.loads(inputs[0])
        return SnowflakeBatchExportInputs(**loaded)

    @workflow.run
    async def run(self, inputs: SnowflakeBatchExportInputs):
        """Workflow implementation to export data to S3 bucket."""
        workflow.logger.info("Starting S3 export")

        data_interval_start, data_interval_end = get_data_interval_from_workflow_inputs(inputs)

        create_export_run_inputs = CreateBatchExportRunInputs(
            team_id=inputs.team_id,
            batch_export_id=inputs.batch_export_id,
            data_interval_start=data_interval_start.isoformat(),
            data_interval_end=data_interval_end.isoformat(),
        )
        run_id = await workflow.execute_activity(
            create_export_run,
            create_export_run_inputs,
            start_to_close_timeout=dt.timedelta(minutes=20),
            schedule_to_close_timeout=dt.timedelta(minutes=5),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["NotNullViolation", "IntegrityError"],
            ),
        )

        update_inputs = UpdateBatchExportRunStatusInputs(id=run_id, status="Completed")

        insert_inputs = SnowflakeInsertInputs(
            team_id=inputs.team_id,
            user=inputs.user,
            password=inputs.password,
            account=inputs.account,
            warehouse=inputs.warehouse,
            database=inputs.database,
            schema=inputs.schema,
            table_name=inputs.table_name,
            data_interval_start=data_interval_start.isoformat(),
            data_interval_end=data_interval_end.isoformat(),
        )
        try:
            await workflow.execute_activity(
                insert_into_snowflake_activity,
                insert_inputs,
                start_to_close_timeout=dt.timedelta(minutes=20),
                schedule_to_close_timeout=dt.timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    non_retryable_error_types=[
                        # If we can't connect to ClickHouse, no point in
                        # retrying.
                        "ConnectionError",
                        # Validation failed, and will keep failing.
                        "ValueError",
                    ],
                ),
            )

        except Exception as e:
            workflow.logger.exception("Snowflake BatchExport failed.", exc_info=e)
            update_inputs.status = "Failed"
            raise

        finally:
            await workflow.execute_activity(
                update_export_run_status,
                update_inputs,
                start_to_close_timeout=dt.timedelta(minutes=20),
                schedule_to_close_timeout=dt.timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )


def get_data_interval_from_workflow_inputs(inputs: SnowflakeBatchExportInputs) -> tuple[dt.datetime, dt.datetime]:
    """Return the start and end of an export's data interval.

    Args:
        inputs: The Snowflake BatchExport inputs.

    Raises:
        TypeError: If when trying to obtain the data interval end we run into non-str types.

    Returns:
        A tuple of two dt.datetime indicating start and end of the
        data_interval.

    TODO: this is pretty much a straight copy of the same function for S3.
    Refactor to not use the destination specific inputs.
    """
    data_interval_end_str = inputs.data_interval_end

    if not data_interval_end_str:
        data_interval_end_search_attr = workflow.info().search_attributes.get("TemporalScheduledStartTime")

        # These two if-checks are a bit pedantic, but Temporal SDK is heavily typed.
        # So, they exist to make mypy happy.
        if data_interval_end_search_attr is None:
            msg = (
                "Expected 'TemporalScheduledStartTime' of type 'list[str]' or 'list[datetime], found 'NoneType'."
                "This should be set by the Temporal Schedule unless triggering workflow manually."
                "In the latter case, ensure 'S3BatchExportInputs.data_interval_end' is set."
            )
            raise TypeError(msg)

        # Failing here would perhaps be a bug in Temporal.
        if isinstance(data_interval_end_search_attr[0], str):
            data_interval_end_str = data_interval_end_search_attr[0]
            data_interval_end = dt.datetime.fromisoformat(data_interval_end_str)

        elif isinstance(data_interval_end_search_attr[0], dt.datetime):
            data_interval_end = data_interval_end_search_attr[0]

        else:
            msg = (
                f"Expected search attribute to be of type 'str' or 'datetime' found '{data_interval_end_search_attr[0]}' "
                f"of type '{type(data_interval_end_search_attr[0])}'."
            )
            raise TypeError(msg)
    else:
        data_interval_end = dt.datetime.fromisoformat(data_interval_end_str)

    # TODO: handle different time intervals. I'm also no 100% happy with how we
    # decide on the interval start/end. I would be much happier if we were to
    # inspect the previous `data_interval_end` and used that as the start of
    # the next interval. This would be more robust to failures and retries.
    data_interval_start = data_interval_end - dt.timedelta(seconds=3600)

    return (data_interval_start, data_interval_end)
