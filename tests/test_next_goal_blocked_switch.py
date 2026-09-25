import unittest

from backend.app.demo.engine import (
    DemoEngine,
    UNACCEPTED_FLIP_SIDE,
    UNACCEPTED_INVALID_SCORE,
    UNACCEPTED_KEEP_SIDE,
    UNACCEPTED_NO_CHANGE,
    resolve_unaccepted_score_transition,
    selected_team_scored_between,
)
from backend.app.demo.models import Score, Scorer
from backend.app.demo.strategy import StrategyConfig, StrategyType
from backend.app.live.executor import BLOCKED_EVENT_SIGNAL
from backend.app.live.models import PlacementObservation


class FakeManager:
    def set_logger(self, _logger):
        pass


class NextGoalUnacceptedRecoveryTests(unittest.TestCase):
    """Regression tests for the simplified LIVE NOT_ACCEPTED strategy.

    The old suite in this file asserted that bookmaker lock detection could
    terminate/switch a LIVE series. That behavior is intentionally obsolete:
    LIVE now uses ACCEPTED + scoreboard delta, while lock information is only a
    technical signal that a coupon was not accepted.
    """

    def test_selected_team_score_delta_is_side_specific(self):
        before = Score(1, 0)
        after_team1 = Score(2, 0)
        after_team2 = Score(1, 1)

        self.assertTrue(
            selected_team_scored_between(
                before,
                after_team1,
                Scorer.TEAM_1,
            )
        )
        self.assertFalse(
            selected_team_scored_between(
                before,
                after_team1,
                Scorer.TEAM_2,
            )
        )
        self.assertTrue(
            selected_team_scored_between(
                before,
                after_team2,
                Scorer.TEAM_2,
            )
        )

    def test_no_score_change_keeps_same_unaccepted_attempt_context(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2,
                Score(1, 0),
                Score(1, 0),
            ),
            UNACCEPTED_NO_CHANGE,
        )

    def test_opponent_goals_keep_selected_side(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2,
                Score(1, 0),
                Score(3, 0),
            ),
            UNACCEPTED_KEEP_SIDE,
        )

    def test_selected_team_goal_flips_side(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2,
                Score(1, 0),
                Score(3, 1),
            ),
            UNACCEPTED_FLIP_SIDE,
        )

    def test_both_teams_scoring_still_flips_when_selected_team_scored(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2,
                Score(1, 0),
                Score(3, 2),
            ),
            UNACCEPTED_FLIP_SIDE,
        )

    def test_backwards_score_is_not_used_for_retry_decision(self):
        self.assertEqual(
            resolve_unaccepted_score_transition(
                Scorer.TEAM_2,
                Score(3, 1),
                Score(2, 1),
            ),
            UNACCEPTED_INVALID_SCORE,
        )

    def test_live_lock_signal_never_enables_lock_driven_strategy(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._config = StrategyConfig.from_payload(
            {
                "strategy_type": StrategyType.NEXT_GOAL.value,
                "blocked_events_switch_enabled": True,
            }
        )
        observation = PlacementObservation(
            False,
            BLOCKED_EVENT_SIGNAL,
            retryable=True,
        )

        self.assertFalse(
            engine._blocked_score_monitoring_enabled(
                observation=observation,
                selected_match={"match_id": "match"},
            )
        )

    def test_demo_configuration_remains_independent_from_live_rule(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "DEMO"
        engine._config = StrategyConfig.from_payload(
            {
                "strategy_type": StrategyType.NEXT_GOAL.value,
                "blocked_events_switch_enabled": True,
            }
        )
        observation = PlacementObservation(
            False,
            BLOCKED_EVENT_SIGNAL,
            retryable=True,
        )

        self.assertTrue(
            engine._blocked_score_monitoring_enabled(
                observation=observation,
                selected_match={"match_id": "match"},
            )
        )

    def test_unaccepted_result_never_counts_as_max_three_loss(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._config = StrategyConfig.from_payload(
            {
                "max_steps": 7,
                "stakes": [10, 22, 48, 105, 231, 508, 1117],
                "max_three_steps_enabled": True,
            }
        )

        self.assertFalse(
            engine._max_three_switch_ready(
                result="NOT_PLACED",
                switch_already_used=False,
                accepted_losses_in_current_match=3,
                step=3,
            )
        )

    def test_three_real_losses_still_trigger_existing_match_switch(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._config = StrategyConfig.from_payload(
            {
                "max_steps": 7,
                "stakes": [10, 22, 48, 105, 231, 508, 1117],
                "max_three_steps_enabled": True,
            }
        )

        self.assertTrue(
            engine._max_three_switch_ready(
                result="LOSE",
                switch_already_used=False,
                accepted_losses_in_current_match=3,
                step=3,
            )
        )


if __name__ == "__main__":
    unittest.main()
