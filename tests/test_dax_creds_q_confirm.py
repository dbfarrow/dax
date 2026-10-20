"""`_q_confirm` disables questionary's `auto_enter` — found live, 2026-10:
every confirm prompt in dax (credential overwrites, destroy/archive
removals, the bootstrap and restore wizards) felt "delicate", because
questionary's own default (`auto_enter=True`) commits a y/n answer on the
keypress alone, with no Enter and no chance to reconsider. Reapplied in the
env-lifecycle consolidation's step 5 (decision 9), alongside `_flush_stdin`
rather than in place of it.
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


def test_still_flushes_stdin_first(monkeypatch):
    import dax_creds.init as init_mod
    order = []
    monkeypatch.setattr(init_mod, '_flush_stdin', lambda: order.append('flush'))
    monkeypatch.setattr('questionary.confirm',
                        lambda prompt, default=False, **kwargs:
                        order.append('ask') or _FakeQuestion(False))

    _q_confirm('Proceed?')

    assert order == ['flush', 'ask']


def test_still_raises_keyboard_interrupt_on_none(monkeypatch):
    monkeypatch.setattr('questionary.confirm',
                        lambda prompt, default=False, **kwargs: _FakeQuestion(None))

    try:
        _q_confirm('Proceed?')
        assert False, 'expected KeyboardInterrupt'
    except KeyboardInterrupt:
        pass
