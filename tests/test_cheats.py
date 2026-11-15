import tempfile
import unittest
from pathlib import Path
from unittest import mock

from armadalive import cheats


class TestCheats(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name)
        patch = mock.patch.object(cheats, "CONFIG", str(self.config))
        patch.start()
        self.addCleanup(patch.stop)

    def test_toggle_cheat(self):
        c = cheats.Cheats()
        c.toggle_cheat("snes/mario", 0)
        self.assertTrue(c.is_active("snes/mario", 0))
        c.toggle_cheat("snes/mario", 0)
        self.assertFalse(c.is_active("snes/mario", 0))

    def test_write_config(self):
        opt_file = self.config / "config" / "Snes9x" / "mario.opt"
        opt_file.parent.mkdir(parents=True)
        opt_file.write_text('snes9x_region = "PAL"\ncheat1_enable = "true"\n')

        c = cheats.Cheats()
        c.toggle_cheat("snes/mario", 0)
        c.toggle_cheat("snes/mario", 2)

        entries = opt_file.read_text().splitlines()
        self.assertIn('snes9x_region = "PAL"', entries)
        self.assertIn('cheats_enabled = "true"', entries)
        self.assertIn('cheat0_enable = "true"', entries)
        self.assertIn('cheat1_enable = "false"', entries)
        self.assertIn('cheat2_enable = "true"', entries)

    def test_non_emulated_games_ignored(self):
        c = cheats.Cheats()
        c.toggle_cheat("steam/570", 0)
        self.assertEqual(list(self.config.rglob("*.opt")), [])

    def test_nested_canonical_game_keeps_the_emulator_option_filename(self):
        key = "esde:snes/Region/Game.1.sfc"
        backend = cheats.Cheats()
        backend.toggle_cheat(key, 2)
        self.assertTrue(backend.is_active(key, 2))
        self.assertFalse(backend.is_active("esde:snes/Other/Game.1.sfc", 2))
        config = self.config / "config" / "Snes9x" / "Game.1.opt"
        self.assertIn('cheat2_enable = "true"', config.read_text())
