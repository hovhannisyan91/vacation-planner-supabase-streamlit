import os
import unittest
from unittest.mock import patch

from planner.sql_repository import database_url_from_env


class SqlRepositoryConfigTests(unittest.TestCase):
    def test_url_builder_handles_special_password_without_manual_escaping(self):
        values = {
            "DB_USER": "postgres",
            "DB_PASSWORD": "p@ss:w%rd/with?reserved",
            "DB_HOST": "db.example.supabase.co",
            "DB_PORT": "5432",
            "DB_NAME": "postgres",
        }
        with patch.dict(os.environ, values, clear=False):
            url = database_url_from_env()
        self.assertEqual(url.drivername, "postgresql+psycopg2")
        self.assertEqual(url.password, values["DB_PASSWORD"])
        self.assertEqual(url.host, values["DB_HOST"])
        self.assertEqual(url.query["sslmode"], "require")

    def test_lowercase_connection_names_from_supabase_example_are_supported(self):
        values = {
            "user": "postgres",
            "password": "secret",
            "host": "db.example.supabase.co",
            "port": "5432",
            "dbname": "postgres",
        }
        names = ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME", "DATABASE_URL")
        with patch.dict(os.environ, values, clear=True):
            for name in names:
                os.environ.pop(name, None)
            url = database_url_from_env()
        self.assertEqual(url.username, "postgres")
        self.assertEqual(url.database, "postgres")


if __name__ == "__main__":
    unittest.main()

