"""`_q_confirm` disables questionary's `auto_enter` — found live, 2026-10:
every confirm prompt in dax (credential overwrites, batch destroy/archive
removals, the bootstrap wizard) felt "delicate" because questionary's own
default (`auto_enter=True`) commits a y/n answer on the keypress alone, with
no Enter and no chance to reconsider. A single stray or too-fast keypress
could advance a multi-step wizard with no recovery.
"""
from dax_creds.init import _q_confirm


class _FakeQuestion:
    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


def test_disables_auto_enter(monkeypatch):
    captured = {}

    def fake_confirm(prompt, default=False, **kwargs):
        captured.update(kwargs)
        captured['prompt'] = prompt
        captured['default'] = default
        return _FakeQuestion(True)

    monkeypatch.setattr('questionary.confirm', fake_confirm)

    assert _q_confirm('Proceed?', default=True) is True
    assert captured['auto_enter'] is False
    assert captured['prompt'] == 'Proceed?'
    assert captured['default'] is True


def test_still_raises_keyboard_interrupt_on_none(monkeypatch):
    monkeypatch.setattr('questionary.confirm',
                        lambda prompt, default=False, **kwargs: _FakeQuestion(None))

    try:
        _q_confirm('Proceed?')
        assert False, 'expected KeyboardInterrupt'
    except KeyboardInterrupt:
        pass
