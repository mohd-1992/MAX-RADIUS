import importlib.util
import io
from pathlib import Path
import unittest

source = Path(__file__).resolve().parents[1] / 'services' / 'mysql_restore_stream.py'
if source.exists():
    spec = importlib.util.spec_from_file_location('restore_stream_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
else:
    from services import mysql_restore_stream as module


def rewrite(value):
    output = io.BytesIO()
    result = module.rebind_dump_definers(io.BytesIO(value), output)
    return output.getvalue(), result['definers_rebound']


class RestoreStreamTests(unittest.TestCase):
    def test_view_owner(self):
        result, count = rewrite(b'CREATE DEFINER=`old`@`%` VIEW v AS SELECT 1;')
        self.assertEqual(result, b'CREATE DEFINER=CURRENT_USER VIEW v AS SELECT 1;')
        self.assertEqual(count, 1)

    def test_native_split_executable_comments(self):
        value = (b'/*!50001 CREATE ALGORITHM=UNDEFINED */\n'
                 b'/*!50013 DEFINER=`old`@`%` SQL SECURITY DEFINER */\n'
                 b'/*!50001 VIEW v AS SELECT 1 */;\n')
        expected = value.replace(b'`old`@`%`', b'CURRENT_USER')
        self.assertEqual(rewrite(value), (expected, 1))

    def test_mariadb_comment(self):
        self.assertEqual(rewrite(b'/*M!100100 CREATE DEFINER=old@localhost VIEW v AS SELECT 1 */;')[1], 1)

    def test_single_double_and_escaped_accounts(self):
        for owner in (b"'o\\'ld'@'local'", b'"old"@"%"', b'`o``ld`@`local`'):
            with self.subTest(owner=owner):
                self.assertEqual(rewrite(b'CREATE DEFINER=' + owner + b' VIEW v AS SELECT 1;')[1], 1)

    def test_insert_values_untouched(self):
        value = b"INSERT INTO t VALUES ('CREATE DEFINER=`a`@`b` VIEW x', '/*!50013 DEFINER=x@y */');\n"
        self.assertEqual(rewrite(value), (value, 0))

    def test_multiline_data_untouched(self):
        value = b"INSERT INTO t VALUES ('line one\nCREATE DEFINER=x@y VIEW v;\nline three');\n"
        self.assertEqual(rewrite(value), (value, 0))

    def test_ordinary_comments_untouched(self):
        value = b'-- CREATE DEFINER=x@y VIEW v;\n/* CREATE DEFINER=x@y VIEW v; */\n# DEFINER=x@y\n'
        self.assertEqual(rewrite(value), (value, 0))

    def test_delimiter_and_routine_body(self):
        value = (b'DELIMITER $$\nCREATE DEFINER=old@localhost PROCEDURE p()\nBEGIN\n'
                 b"SELECT 'DEFINER=other@host';\nEND$$\nDELIMITER ;\n")
        self.assertEqual(rewrite(value), (value.replace(b'old@localhost', b'CURRENT_USER'), 1))

    def test_double_semicolon_delimiter(self):
        value = b'DELIMITER ;;\nCREATE DEFINER=x@y FUNCTION f() RETURNS INT RETURN 1;;\nDELIMITER ;\n'
        self.assertEqual(rewrite(value)[1], 1)

    def test_current_user_forms_unchanged(self):
        for owner in (b'CURRENT_USER', b'CURRENT_USER()'):
            value = b'CREATE DEFINER=' + owner + b' VIEW v AS SELECT 1;'
            self.assertEqual(rewrite(value), (value, 0))

    def test_whitespace_comments_preserved(self):
        value = b'CREATE DEFINER /* keep */ =\n `old` /* keep2 */ @ `host` VIEW v AS SELECT 1;'
        result, count = rewrite(value)
        self.assertEqual(count, 1)
        self.assertIn(b'/* keep */', result)
        self.assertIn(b'/* keep2 */', result)

    def test_accounts_across_executable_wrappers(self):
        value = b"/*!50001 CREATE DEFINER='old' */ /*!50013 @'host' */ VIEW v AS SELECT 1;"
        result, count = rewrite(value)
        self.assertEqual(count, 1)
        self.assertEqual(result.count(b'/*!'), 2)
        self.assertEqual(result.count(b'*/'), 2)

    def test_quoted_definer_column_untouched(self):
        value = b'CREATE TABLE t (`DEFINER` VARCHAR(20));'
        self.assertEqual(rewrite(value), (value, 0))

    def test_alter_view(self):
        self.assertEqual(rewrite(b'ALTER DEFINER=x@y VIEW v AS SELECT 2;')[1], 1)

    def test_all_routine_kinds(self):
        for kind in (b'FUNCTION', b'PROCEDURE', b'TRIGGER', b'EVENT'):
            self.assertEqual(rewrite(b'CREATE DEFINER=x@y ' + kind + b' object body;')[1], 1)

    def test_malformed_owner_fails(self):
        for tail in (b'x', b'x@', b'=', b"'x", b'CURRENT_USER(', b'CURRENT_USER'):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                rewrite(b'CREATE DEFINER=' + tail)

    def test_truncated_quote_or_comment_fails(self):
        for value in (b"INSERT INTO t VALUES ('unfinished", b'/* unfinished'):
            with self.assertRaises(ValueError):
                rewrite(value)

    def test_unsupported_object_fails(self):
        with self.assertRaises(ValueError):
            rewrite(b'CREATE DEFINER=x@y TABLE t (id INT);')

    def test_unsupported_escape_mode_fails_before_import(self):
        with self.assertRaises(ValueError):
            rewrite(b"SET SQL_MODE='NO_BACKSLASH_ESCAPES';")

    def test_bounded_token(self):
        old = module.MAX_TOKEN
        try:
            module.MAX_TOKEN = 32
            with self.assertRaises(ValueError):
                rewrite(b"INSERT INTO t VALUES ('" + b'x' * 40 + b"');")
        finally:
            module.MAX_TOKEN = old

    def test_second_pass_idempotent(self):
        value, _ = rewrite(b'CREATE DEFINER=x@y VIEW v AS SELECT 1;')
        self.assertEqual(rewrite(value), (value, 0))

    def test_utf8_bom_preserved(self):
        value = b'\xef\xbb\xbfCREATE DEFINER=x@y VIEW v AS SELECT 1;'
        self.assertEqual(rewrite(value), (value.replace(b'x@y', b'CURRENT_USER'), 1))

    def test_bom_before_delimiter(self):
        value = b'\xef\xbb\xbfDELIMITER $$\nCREATE DEFINER=x@y PROCEDURE p() BEGIN SELECT 1; END$$\n'
        self.assertEqual(rewrite(value), (value.replace(b'x@y', b'CURRENT_USER'), 1))

    def test_insert_then_ddl_on_same_line_is_not_fast_copied(self):
        value = b"INSERT INTO t VALUES ('ok'); CREATE DEFINER=x@y VIEW v AS SELECT 1;\n"
        self.assertEqual(rewrite(value), (value.replace(b'x@y', b'CURRENT_USER'), 1))

    def test_insert_comment_cannot_hide_another_statement(self):
        value = b"INSERT INTO t VALUES (1) /* ' */; CREATE DEFINER=x@y VIEW v AS SELECT 'literal';\n"
        self.assertEqual(rewrite(value), (value.replace(b'x@y', b'CURRENT_USER'), 1))

    def test_insert_fast_copy_preserves_quoted_end_and_comment_text(self):
        value = b"INSERT INTO t VALUES ('; CREATE DEFINER=x@y /* text */ -- #', 'a\\\'b', 'a''b');\n"
        self.assertTrue(module._single_insert(value))
        self.assertEqual(rewrite(value), (value, 0))

    def test_truncated_insert_is_not_fast_copied(self):
        with self.assertRaises(ValueError):
            rewrite(b"INSERT INTO t VALUES ('unfinished);\n")


if __name__ == '__main__':
    unittest.main()
