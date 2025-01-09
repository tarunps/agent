from __future__ import annotations

import os
import subprocess

import peewee
import requests

from agent.database_server import DatabaseServer
from agent.job import job, step


class DatabasePhysicalBackup(DatabaseServer):
    def __init__(
        self,
        databases: list[str],
        db_user: str,
        db_password: str,
        snapshot_trigger_url: str,
        db_host: str = "localhost",
        db_port: int = 3306,
        db_base_path: str = "/var/lib/mysql",
    ):
        if not databases:
            raise ValueError("At least one database is required")
        # Instance variable for internal use
        self._db_instances: dict[str, peewee.MySQLDatabase] = {}
        self._db_tables_locked: dict[str, bool] = {db: False for db in databases}

        # variables
        self.snapshot_trigger_url = snapshot_trigger_url
        self.databases = databases
        self.db_user = db_user
        self.db_password = db_password
        self.db_host = db_host
        self.db_port = db_port
        self.db_base_path = db_base_path
        self.db_directories: dict[str, str] = {
            db: os.path.join(self.db_base_path, db) for db in self.databases
        }

        self.innodb_tables: dict[str, list[str]] = {db: [] for db in self.databases}
        self.myisam_tables: dict[str, list[str]] = {db: [] for db in self.databases}
        self.table_schemas: dict[str, str] = {}

    @job("Physical Backup Database", priority="low")
    def backup_job(self):
        self.fetch_table_info()
        self.flush_tables()
        self.flush_changes_to_disk()
        self.validate_exportable_files()
        self.export_table_schemas()
        self.export_collected_metadata()
        self.create_snapshot()  # Blocking call
        self.unlock_all_tables()

    @step("Fetch Database Tables Information")
    def fetch_table_info(self):
        """
        Store the table names and their engines in the respective dictionaries
        """
        for db_name in self.databases:
            db_instance = self.get_db(db_name)
            query = (
                "SELECT table_name, ENGINE FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type != 'VIEW' "
                "ORDER BY table_name"
            )
            data = db_instance.execute_sql(query).fetchall()
            for row in data:
                table = row[0]
                engine = row[1]
                if engine == "InnoDB":
                    self.innodb_tables[db_name].append(table)
                elif engine == "MyISAM":
                    self.myisam_tables[db_name].append(table)

    @step("Flush Database Tables")
    def flush_tables(self):
        for db_name in self.databases:
            """
            InnoDB and MyISAM tables
            Flush the tables and take read lock

            Ref : https://mariadb.com/kb/en/flush-tables-for-export/#:~:text=If%20FLUSH%20TABLES%20...%20FOR%20EXPORT%20is%20in%20effect%20in%20the%20session%2C%20the%20following%20statements%20will%20produce%20an%20error%20if%20attempted%3A

            FLUSH TABLES ... FOR EXPORT
            This will
                - Take READ lock on the tables
                - Flush the tables
                - Will not allow to change table structure (ALTER TABLE, DROP TABLE nothing will work)
            """
            tables = self.innodb_tables[db_name] + self.myisam_tables[db_name]
            flush_table_export_query = "FLUSH TABLES {} FOR EXPORT;".format(", ".join(tables))
            self.get_db(db_name).execute_sql(flush_table_export_query)
            self._db_tables_locked[db_name] = True

    @step("Flush Changes to Disk")
    def flush_changes_to_disk(self):
        """
        It's important to flush all the disk buffer of files to disk before snapshot.
        This will ensure that the snapshot is consistent.
        """
        for db_name in self.databases:
            files = os.listdir(self.db_directories[db_name])
            for file in files:
                file_path = os.path.join(self.db_directories[db_name], file)
                with open(file_path, "r") as f:
                    os.fsync(f.fileno())

    @step("Validate Exportable Files")
    def validate_exportable_files(self):
        for db_name in self.databases:
            # list all the files in the database directory
            db_files = os.listdir(self.db_directories[db_name])
            """
            InnoDB tables should have the .cfg files to be able to restore it back

            https://mariadb.com/kb/en/innodb-file-per-table-tablespaces/#exporting-transportable-tablespaces-for-non-partitioned-tables
            """
            for table in self.innodb_tables[db_name]:
                table_file = table + ".ibd"
                if table_file not in db_files:
                    raise DatabaseExportFileNotFoundError(f"IBD file for table {table} not found")
            """
            MyISAM tables should have .MYD and .MYI files at-least to be able to restore it back
            """
            for table in self.myisam_tables[db_name]:
                table_files = [table + ".MYD", table + ".MYI"]
                for table_file in table_files:
                    if table_file not in db_files:
                        raise DatabaseExportFileNotFoundError(f"MYD or MYI file for table {table} not found")

    @step("Export Table Schema")
    def export_table_schemas(self):
        for db_name in self.databases:
            """
            Export the database schema
            It's important to export the schema only after taking the read lock.
            """
            self.table_schemas[db_name] = self.export_table_schema(db_name)

    @step("Export Collected Metadata")
    def export_collected_metadata(self):
        data = {}
        for db_name in self.databases:
            data[db_name] = {
                "innodb_tables": self.innodb_tables[db_name],
                "myisam_tables": self.myisam_tables[db_name],
                "table_schemas": self.table_schemas[db_name],
            }
        return data

    @step("Create Database Snapshot")
    def create_snapshot(self):
        """
        Trigger the snapshot creation
        """
        response = requests.post(self.snapshot_trigger_url)
        response.raise_for_status()

    @step("Unlock Tables")
    def unlock_all_tables(self):
        for db_name in self.databases:
            self._unlock_tables(db_name)

    def export_table_schema(self, db_name) -> str:
        command = [
            "mariadb-dump",
            "-u",
            self.db_user,
            "-p" + self.db_password,
            "--no-data",
            db_name,
        ]
        try:
            output = subprocess.check_output(command)
        except subprocess.CalledProcessError as e:
            raise DatabaseSchemaExportError(e.output)  # noqa: B904

        return output.decode("utf-8")

    def _unlock_tables(self, db_name):
        self.get_db(db_name).execute_sql("UNLOCK TABLES;")
        self._db_tables_locked[db_name] = False
        """
        Anyway, if the db connection gets closed or db thread dies,
        the tables will be unlocked automatically
        """

    def get_db(self, db_name: str) -> peewee.MySQLDatabase:
        instance = self._db_instances.get(db_name, None)
        if instance is not None:
            if not instance.is_connection_usable():
                raise DatabaseConnectionClosedWithDatabase(
                    f"Database connection closed with database {db_name}"
                )
            return instance
        if db_name not in self.databases:
            raise ValueError(f"Database {db_name} not found")
        self._db_instances[db_name] = peewee.MySQLDatabase(
            db_name,
            user=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
        )
        self._db_instances[db_name].connect()
        # Set session wait timeout to 4 hours [EXPERIMENTAL]
        self._db_instances[db_name].execute_sql("SET SESSION wait_timeout = 14400;")
        return self._db_instances[db_name]

    def __del__(self):
        for db_name in self.databases:
            if self._db_tables_locked[db_name]:
                self._unlock_tables(db_name)

        for db_name in self.databases:
            self.get_db(db_name).close()


class DatabaseSchemaExportError(Exception):
    pass


class DatabaseExportFileNotFoundError(Exception):
    pass


class DatabaseConnectionClosedWithDatabase(Exception):
    pass
