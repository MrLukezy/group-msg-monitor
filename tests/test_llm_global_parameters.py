from __future__ import annotations

import unittest

from app.settings_store import (
    LlmGlobalSettings,
    LlmMonitorConfig,
    resolve_llm_timing,
)


class LlmGlobalParameterTests(unittest.TestCase):
    def test_group_inherits_global_timing_by_default(self) -> None:
        global_settings = LlmGlobalSettings(
            default_every_minutes=15,
            default_window_minutes=30,
            default_min_messages=4,
        )
        group_settings = LlmMonitorConfig(
            every_minutes=90,
            window_minutes=120,
            min_messages=20,
        )

        self.assertEqual(
            resolve_llm_timing(global_settings, group_settings),
            (15, 30, 4),
        )

    def test_group_can_override_all_timing_parameters(self) -> None:
        global_settings = LlmGlobalSettings(
            default_every_minutes=15,
            default_window_minutes=30,
            default_min_messages=4,
        )
        group_settings = LlmMonitorConfig(
            use_global_defaults=False,
            every_minutes=45,
            window_minutes=90,
            min_messages=12,
        )

        self.assertEqual(
            resolve_llm_timing(global_settings, group_settings),
            (45, 90, 12),
        )


if __name__ == "__main__":
    unittest.main()
