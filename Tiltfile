# Local Tilt loop: pytest + windowed mpv. No Kubernetes, no Pi deploy.
load('ext://uibutton', 'cmd_button')

python = './.venv/bin/python'
pytest = './.venv/bin/pytest'
control = 'http://127.0.0.1:8765'

local_resource(
    'test',
    cmd=pytest,
    deps=['nostalgiabox', 'tests', 'pyproject.toml'],
    ignore=['**/__pycache__', '**/*.pyc'],
    labels=['dev'],
)

local_resource(
    'tv',
    cmd=[python, '-m', 'nostalgiabox', '--generate-assets'],
    serve_cmd=[python, '-m', 'nostalgiabox', '--config', 'config.tilt.yaml'],
    serve_env={'DYLD_FALLBACK_LIBRARY_PATH': '/opt/homebrew/lib'},
    deps=['nostalgiabox', 'config.tilt.yaml'],
    ignore=['**/__pycache__', '**/*.pyc'],
    readiness_probe=probe(
        http_get=http_get_action(port=8765, host='127.0.0.1', path='/health'),
        period_secs=1,
        timeout_secs=2,
        failure_threshold=30,
    ),
    labels=['dev'],
)

def _post(name, path, text, icon):
    cmd_button(
        name,
        argv=['curl', '-sf', '-X', 'POST', control + path],
        resource='tv',
        text=text,
        icon_name=icon,
    )

_post('ch-up', '/channel/up', 'CH+', 'keyboard_arrow_up')
_post('ch-down', '/channel/down', 'CH-', 'keyboard_arrow_down')
_post('vol-up', '/volume/up', 'Vol+', 'volume_up')
_post('vol-down', '/volume/down', 'Vol-', 'volume_down')
_post('mute', '/mute', 'Mute', 'volume_off')
_post('info', '/info', 'Info', 'info')
_post('last', '/last', 'Last', 'undo')
_post('power', '/power', 'Power', 'power_settings_new')
_post('quit', '/quit', 'Quit', 'close')
