"""docs/help.md is the checked-in render of the live help index (just help-doc)."""

import unittest
from pathlib import Path

from lattice import help as lattice_help

DOC = Path(__file__).resolve().parents[1] / "docs" / "help.md"


@unittest.skipUnless(
    DOC.exists(), "docs/help.md not generated yet (run: just help-doc)"
)
class HelpDocSyncTests(unittest.TestCase):
    def test_help_doc_matches_live_index(self) -> None:
        self.assertEqual(
            DOC.read_text(encoding="utf-8"), lattice_help.render_index() + "\n"
        )


if __name__ == "__main__":
    unittest.main()
