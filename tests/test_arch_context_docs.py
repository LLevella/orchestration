"""The architecture-context block: what an author is told already exists.

Project-agnostic. Everything here runs against a temporary tree, never the real corpus.

Why this file exists: until 2026-09-19 the block globbed ONE directory NON-recursively.
On a project whose designs live one level down (`doc/architecture/<line>/*.md`) that
matched none of them, and one unrelated file that did sit at the top level became the
whole of "existing architecture" for every architecture run. The tests below pin the
three properties that failure violated - recursion, several roots, and an index that
names every document - plus the newest-first ordering the block promises.
"""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.config import RunContext


class _Ctx:
    """The slice of RunContext that read_existing_arch actually touches."""

    ARCH_EXCERPTS = RunContext.ARCH_EXCERPTS
    ARCH_EXCERPT_CHARS = RunContext.ARCH_EXCERPT_CHARS
    ARCH_INDEX_MAX = RunContext.ARCH_INDEX_MAX

    def __init__(self, roots):
        self.docs_arch = list(roots)

    read = RunContext.read_existing_arch


class TestArchContextDocs(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, rel, text, age_s=0):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if age_s:
            t = time.time() - age_s
            import os
            os.utime(p, (t, t))
        return p

    def test_finds_documents_in_subdirectories(self):
        self._write("arch/line-a/ARCH-one.md", "# One\nbody")
        self._write("arch/line-a/deeper/ARCH-two.md", "# Two\nbody")
        out = _Ctx([self.root / "arch"]).read()
        self.assertIn("ARCH-one.md", out)
        self.assertIn("ARCH-two.md", out, "a document one level further down must still be found")

    def test_several_roots_are_merged(self):
        self._write("arch/ts/ARCH-ts.md", "# TS\nbody")
        self._write("arch/adr/ADR-0001.md", "# ADR\nbody")
        out = _Ctx([self.root / "arch/ts", self.root / "arch/adr"]).read()
        self.assertIn("ARCH-ts.md", out)
        self.assertIn("ADR-0001.md", out)

    def test_index_names_every_document_not_only_the_excerpted_ones(self):
        for i in range(RunContext.ARCH_EXCERPTS + 4):
            self._write(f"arch/line/DOC-{i:02d}.md", f"# Doc {i}\nbody")
        out = _Ctx([self.root / "arch"]).read()
        for i in range(RunContext.ARCH_EXCERPTS + 4):
            self.assertIn(f"DOC-{i:02d}.md", out)
        self.assertEqual(out.count("### "), RunContext.ARCH_EXCERPTS,
                         "only the newest few are excerpted, but all are indexed")

    def test_newest_first(self):
        self._write("arch/line/OLD.md", "# Old\nbody", age_s=9000)
        self._write("arch/line/NEW.md", "# New\nbody")
        out = _Ctx([self.root / "arch"]).read()
        self.assertLess(out.index("NEW.md"), out.index("OLD.md"))

    def test_excerpt_is_bounded(self):
        self._write("arch/line/BIG.md", "# Big\n" + ("x" * (RunContext.ARCH_EXCERPT_CHARS * 3)))
        out = _Ctx([self.root / "arch"]).read()
        self.assertLess(out.count("x"), RunContext.ARCH_EXCERPT_CHARS + 10)

    def test_labels_carry_the_directory(self):
        self._write("arch/ts/ARCH-ts.md", "# TS\nbody")
        out = _Ctx([self.root / "arch/ts"]).read()
        self.assertIn("ts/ARCH-ts.md", out,
                      "the label must be readable as a path, so an author can go read it")

    def test_missing_roots_report_themselves(self):
        out = _Ctx([self.root / "nope"]).read()
        self.assertIn("no existing architecture docs directory", out)
        self.assertIn("nope", out, "say WHICH path is missing")

    def test_empty_roots_say_so(self):
        (self.root / "arch").mkdir()
        out = _Ctx([self.root / "arch"]).read()
        self.assertIn("no .md files", out)

    def test_unreadable_file_does_not_abort_the_run(self):
        self._write("arch/line/OK.md", "# Ok\nbody")
        (self.root / "arch/line/dangling.md").symlink_to(self.root / "gone.md")
        out = _Ctx([self.root / "arch"]).read()
        self.assertIn("OK.md", out)


if __name__ == "__main__":
    unittest.main()
