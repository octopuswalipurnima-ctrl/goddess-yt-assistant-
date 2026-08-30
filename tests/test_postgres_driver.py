"""Regression guard for Railway's default SQLAlchemy PostgreSQL dialect."""
import unittest


class PostgreSQLDriverTests(unittest.TestCase):
    def test_psycopg2_driver_is_importable(self):
        import psycopg2
        self.assertTrue(psycopg2.__version__)


if __name__ == "__main__":
    unittest.main()
