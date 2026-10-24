import argparse
import glob
import io
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT


class HelpStrings(unittest.TestCase):
    def test_every_help_string_formats(self):
        """Python 3.14 checks each help string when the option is added: a bare % stops the script."""
        bad = []
        add = argparse._ActionsContainer.add_argument

        def checked(container, *a, **k):
            action = add(container, *a, **k)
            if action.help:
                try:
                    action.help % dict(vars(action), prog="x")
                except (TypeError, ValueError, KeyError) as e:
                    bad.append((action.option_strings, action.help, e))
            return action

        argparse._ActionsContainer.add_argument = checked
        try:
            for path in sorted(glob.glob(os.path.join(ROOT, "trader-*", "*.py"))):
                folder, name = os.path.basename(os.path.dirname(path)), os.path.basename(path)[:-3]
                mod = load(folder, name)
                if not hasattr(mod, "main"):
                    continue
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        mod.main(["--help"])
                    except SystemExit:
                        pass
        finally:
            argparse._ActionsContainer.add_argument = add
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
