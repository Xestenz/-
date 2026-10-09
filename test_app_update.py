import subprocess
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app_update import update_checkout


class UpdateTests(unittest.TestCase):
    def test_preserves_existing_ssh_configuration(self):
        for configured in (None, 'custom-ssh -i employee-key'):
            env = {} if configured is None else {'GIT_SSH_COMMAND': configured}
            results = [Mock(returncode=0, stdout=value, stderr='')
                       for value in ('main', '', 'abc', '', 'abc')]
            with patch.dict(os.environ, env, clear=True), \
                    patch('app_update.subprocess.run', side_effect=results) as run:
                self.assertFalse(update_checkout('.')[0])
            for call in run.call_args_list:
                actual = call.kwargs['env']
                self.assertEqual(actual.get('GIT_SSH_COMMAND'), configured)
                if configured is None:
                    self.assertNotIn('GIT_SSH_COMMAND', actual)

    def test_restart_is_scheduled_only_after_successful_update(self):
        import waybill_app as app
        request = Mock(headers={'X-Update-Token': app._update_token})
        with patch.object(app, '_server', Mock()), patch.object(app, '_updating', False), \
                patch.object(app, '_restart_requested', False), patch.object(app, '_active_requests', 0), \
                patch.object(app, '_queued_paths', {}), patch.object(app, 'waybills', {}), \
                patch.object(app, '_save_state'), patch.object(app.threading, 'Timer') as timer, \
                patch.object(app, 'update_checkout', return_value=(True, 'abcdefg')):
            self.assertTrue(app.update_app(request)['restart'])
            self.assertTrue(app._restart_requested)
            timer.return_value.start.assert_called_once()

    def test_update_noop_and_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            def git(where, *args):
                return subprocess.run(['git', *args], cwd=where, check=True, capture_output=True)
            git(source, 'init', '-b', 'main')
            git(source, 'config', 'user.email', 'test@example.com')
            git(source, 'config', 'user.name', 'Test')
            (source / '.gitignore').write_text('.env\n')
            (source / 'app.py').write_text('version = 1\n')
            git(source, 'add', '.')
            git(source, 'commit', '-m', 'initial')
            local = root / 'employee'
            git(root, 'clone', str(source), str(local))
            (local / '.env').write_text('LOCAL_SETTING=keep')
            self.assertFalse(update_checkout(local)[0])
            (source / 'app.py').write_text('version = 2\n')
            git(source, 'commit', '-am', 'update')
            self.assertTrue(update_checkout(local)[0])
            self.assertEqual((local / 'app.py').read_text(), 'version = 2\n')
            self.assertEqual((local / '.env').read_text(), 'LOCAL_SETTING=keep')
            (local / 'app.py').write_text('local modification')
            with self.assertRaisesRegex(RuntimeError, 'местные изменения'):
                update_checkout(local)
            (local / 'app.py').write_text('version = 2\n')
            (source / 'requirements.txt').write_text('new-dependency\n')
            git(source, 'add', '.')
            git(source, 'commit', '-m', 'dependencies')
            with self.assertRaisesRegex(RuntimeError, 'зависимости'):
                update_checkout(local)
            self.assertFalse((local / 'requirements.txt').exists())


if __name__ == '__main__':
    unittest.main()
