"""Fast-forward updates without shell commands or overwriting local edits."""
import os
import subprocess
from pathlib import Path


def update_checkout(root):
    root = Path(root)
    def git(*args):
        # Preserve the SSH executable, identity and agent configured for normal
        # git pull (including core.sshCommand and inherited GIT_SSH_COMMAND).
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        result = subprocess.run(['git', *args], cwd=root, env=env,
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('Git: ' + (result.stderr or result.stdout).strip()[-1500:])
        return result.stdout.strip()
    if git('branch', '--show-current') != 'main':
        raise RuntimeError('Обновление доступно только для ветки main.')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Есть местные изменения программы. Обновление отменено, обратитесь к разработчику.')
    before = git('rev-parse', 'HEAD')
    git('fetch', 'origin', 'main')
    target = git('rev-parse', 'FETCH_HEAD')
    if before == target:
        return False, before[:7]
    git('merge-base', '--is-ancestor', before, target)
    changed = git('diff', '--name-only', before, target).splitlines()
    protected = {'.env', 'waybills_state.json', 'field_position_overrides.json', 'monitor_pl_queue.json'}
    if any(p in protected or p.startswith(('uploads/', 'completed/')) for p in changed):
        raise RuntimeError('Обновление затрагивает местные настройки или документы. Нужна проверка разработчика.')
    if any(Path(p).name in {'requirements.txt', 'pyproject.toml', 'poetry.lock'} for p in changed):
        raise RuntimeError('Изменились зависимости. Это обновление должен установить разработчик.')
    for path in changed:
        if path.endswith('.py') and git('ls-tree', '--name-only', target, '--', path):
            compile(git('show', target + ':' + path), path, 'exec')
    git('merge', '--ff-only', target)
    return True, target[:7]
