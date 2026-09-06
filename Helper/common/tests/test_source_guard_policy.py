"""A guard that reads source freezes the layout. New ones must say what ends them.

179 tests in this suite assert on SOURCE CODE rather than on behaviour —
`inspect.getsource`, `ast.parse`, `read_text` on a module. Almost every one of
them earned its place: they exist because this codebase's most frequent and
most expensive bug shape is an invariant enforced in two places and implemented
in one, and no behavioural test can catch a rule that was silently dropped from
its second copy. The design reviewer of 2026-08-29 called them out anyway, and
was right about the cost rather than the value:

    a source guard freezes the FILE LAYOUT, not the invariant. It fails when
    somebody moves a function, renames a variable, or splits a module — and it
    fails identically whether the change broke the rule or merely relocated
    it. Enough of them and every structural improvement costs a day of triage,
    so the structural improvements stop happening.

They are also permanent by default. A behavioural test retires when the
behaviour goes; a source guard outlives the reason it was written and nobody
can tell which ones still matter, because the reason lives in the head of
whoever wrote it.

THE RULE
--------
**A new source guard must name the structural change that retires it**, in a
`RETIRES WHEN:` line in its docstring. Not a promise to delete it — a
description of the world in which it is no longer needed, written while the
author still knows.

Enforced only for guards added after 2026-08-30. The 161 that predate this file
are listed in `BASELINE` below, and that list may only ever SHRINK: removing a
guard, or annotating one, is what closes the entry. It is deliberately a list
of names rather than a count, so the failure message can say WHICH guard is
undeclared instead of "there is one more than there was".

WHY A BASELINE AND NOT A SWEEP
------------------------------
Writing 161 retirement conditions in one pass would be 161 guesses about tests
whose authors are not in the room, and a guess dressed as documentation is
worse than a blank. The debt is recorded, it cannot grow, and it is paid down
by whoever next touches each file — which is also the only person who can pay
it correctly.

Run:  cd Helper && python -m pytest common/tests/test_source_guard_policy.py -v
"""
import ast
import io
import pathlib
import sys

import pytest

HELPER = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

#: What makes a test a SOURCE guard rather than a behavioural one.
MARKERS = ('inspect.getsource', 'inspect.getsourcefile', 'ast.parse',
           'read_text(')

ROOTS = ('bcs/tests', 'zebra/tests', 'common/tests')

#: The declaration a new guard owes. Deliberately a sentence the author has to
#: write rather than a decorator they can copy: the value is entirely in having
#: thought about what would make the guard unnecessary.
MARKER_PHRASE = 'RETIRES WHEN:'

#: The 161 guards that predate this policy. May only SHRINK.
BASELINE = {
    # bcs/tests/test_alert_policy.py
    ('bcs/tests/test_alert_policy.py', 'test_adding_a_safety_class_to_SILENT_breaks_the_import'),
    ('bcs/tests/test_alert_policy.py', 'test_every_manual_attention_alert_is_classified_safety'),
    ('bcs/tests/test_alert_policy.py', 'test_the_vet_and_zebra_senders_are_untouched_by_this_policy'),
    # bcs/tests/test_b21_floor_calls_and_puts.py
    ('bcs/tests/test_b21_floor_calls_and_puts.py', 'test_the_missing_price_disable_is_an_explicit_branch_not_a_caught_error'),
    # bcs/tests/test_b23_misfiled_record.py
    ('bcs/tests/test_b23_misfiled_record.py', 'test_every_store_tag_has_leg_types'),
    ('bcs/tests/test_b23_misfiled_record.py', 'test_the_monitor_does_not_silently_reinterpret_the_record'),
    # bcs/tests/test_b6_b7_adversarial.py
    ('bcs/tests/test_b6_b7_adversarial.py', 'test_the_rail_still_allows_writes_outside_the_real_logs_dir'),
    ('bcs/tests/test_b6_b7_adversarial.py', 'test_every_high_water_write_happens_under_the_lock'),
    # bcs/tests/test_b6_store_locking.py
    ('bcs/tests/test_b6_store_locking.py', 'test_no_store_persists_outside_the_mutate_block'),
    # bcs/tests/test_b7_quarantine_and_ids.py
    ('bcs/tests/test_b7_quarantine_and_ids.py', 'test_the_backup_named_in_the_marker_holds_the_corrupt_bytes'),
    ('bcs/tests/test_b7_quarantine_and_ids.py', 'test_every_all_closed_exit_checks_for_a_quarantine_first'),
    ('bcs/tests/test_b7_quarantine_and_ids.py', 'test_every_call_site_passes_all_three_books'),
    # bcs/tests/test_b9_b12_spot_failures.py
    ('bcs/tests/test_b9_b12_spot_failures.py', 'test_the_outer_handlers_use_the_shared_predicate'),
    ('bcs/tests/test_b9_b12_spot_failures.py', 'test_an_auth_error_in_the_spot_fetch_is_reraised_not_swallowed'),
    ('bcs/tests/test_b9_b12_spot_failures.py', 'test_a_non_auth_spot_failure_is_tracked_per_trade'),
    ('bcs/tests/test_b9_b12_spot_failures.py', 'test_the_success_path_also_reports_so_the_alarm_can_clear'),
    ('bcs/tests/test_b9_b12_spot_failures.py', 'test_the_error_budget_is_reset_at_the_bottom_of_the_loop'),
    ('bcs/tests/test_b9_b12_spot_failures.py', 'test_the_startup_positions_call_is_guarded'),
    # bcs/tests/test_cron_wiring.py
    ('bcs/tests/test_cron_wiring.py', 'test_close_leg_refuses_to_place_while_the_book_is_unreadable'),
    ('bcs/tests/test_cron_wiring.py', 'test_adopted_pending_order_is_cancelled_before_a_replacement'),
    # bcs/tests/test_d4_fh_manual_and_bounds.py
    ('bcs/tests/test_d4_fh_manual_and_bounds.py', 'test_no_route_from_close_fh_position_to_an_order'),
    ('bcs/tests/test_d4_fh_manual_and_bounds.py', 'test_the_marker_survives_the_round_trip_to_disk'),
    # bcs/tests/test_dry_run_close_alert.py
    ('bcs/tests/test_dry_run_close_alert.py', 'test_both_close_paths_render_through_one_formatter'),
    # bcs/tests/test_entry_executor.py
    ('bcs/tests/test_entry_executor.py', 'test_the_gate_is_read_strictly'),
    ('bcs/tests/test_entry_executor.py', 'test_the_suite_pins_the_clock_it_gates_on'),
    # bcs/tests/test_exit_bridge_real_store.py
    ('bcs/tests/test_exit_bridge_real_store.py', 'test_the_adapter_reads_the_keys_the_monitor_actually_writes'),
    # bcs/tests/test_exit_guards.py
    ('bcs/tests/test_exit_guards.py', 'test_reconcile_is_wired_into_the_success_path'),
    ('bcs/tests/test_exit_guards.py', 'test_the_two_engines_bound_a_negative_the_same_way'),
    # bcs/tests/test_exit_vet.py
    ('bcs/tests/test_exit_vet.py', 'test_the_real_gate_accepts_what_this_module_sends'),
    ('bcs/tests/test_exit_vet.py', 'test_the_order_path_never_blocks_in_line_for_a_verdict'),
    # bcs/tests/test_expiry_margin.py
    ('bcs/tests/test_expiry_margin.py', 'test_warning_is_wired_into_the_cron_startup'),
    ('bcs/tests/test_expiry_margin.py', 'test_note_poll_is_wired_into_both_loops'),
    ('bcs/tests/test_expiry_margin.py', 'test_the_call_sites_pass_a_float_not_the_dict'),
    # bcs/tests/test_kill_switch.py
    ('bcs/tests/test_kill_switch.py', 'test_the_shipped_tracked_switch_is_armed_and_secret_free'),
    # bcs/tests/test_m14_clearance_and_reporting.py
    ('bcs/tests/test_m14_clearance_and_reporting.py', 'test_the_cli_exposes_all_three_verbs'),
    # bcs/tests/test_m14_close_failure_schema.py
    ('bcs/tests/test_m14_close_failure_schema.py', 'test_the_sweep_may_still_update_a_record_it_did_not_freeze'),
    # bcs/tests/test_m14_recovery_decisions.py
    ('bcs/tests/test_m14_recovery_decisions.py', 'test_the_attempt_count_cannot_be_widened_past_the_ceiling'),
    ('bcs/tests/test_m14_recovery_decisions.py', 'test_a_nonsense_attempt_count_does_not_silently_disable_the_close'),
    # bcs/tests/test_m14_recovery_sweep.py
    ('bcs/tests/test_m14_recovery_sweep.py', 'test_the_sweep_is_wired_into_the_poll_loop_after_the_per_trade_work'),
    ('bcs/tests/test_m14_recovery_sweep.py', 'test_the_fh_book_is_wired_with_orders_disabled'),
    # bcs/tests/test_m7_ist_clock.py
    ('bcs/tests/test_m7_ist_clock.py', 'test_no_module_on_the_exchange_path_reads_the_box_clock'),
    ('bcs/tests/test_m7_ist_clock.py', 'test_no_naive_wall_clock_reads_are_left_on_the_session_gates'),
    # bcs/tests/test_n14_approximate_pnl.py
    ('bcs/tests/test_n14_approximate_pnl.py', 'test_the_live_cohort_has_no_approximate_exits_today'),
    # bcs/tests/test_order_journal.py
    ('bcs/tests/test_order_journal.py', 'test_every_frozen_book_label_comes_from_the_one_table'),
    ('bcs/tests/test_order_journal.py', 'test_place_limit_order_is_still_the_only_order_choke_point'),
    # bcs/tests/test_paper_position_check.py
    ('bcs/tests/test_paper_position_check.py', 'test_the_startup_sweep_routes_through_the_one_function'),
    # bcs/tests/test_paper_vs_live_close.py
    ('bcs/tests/test_paper_vs_live_close.py', 'test_the_auto_entry_path_stamps_placed_at_broker_only_when_not_dry'),
    ('bcs/tests/test_paper_vs_live_close.py', 'test_every_bcs_entry_path_stamps_the_flag'),
    ('bcs/tests/test_paper_vs_live_close.py', 'test_the_paper_guard_sits_on_the_only_route_to_an_order'),
    ('bcs/tests/test_paper_vs_live_close.py', 'test_no_call_site_turns_a_missing_fill_back_into_zero'),
    ('bcs/tests/test_paper_vs_live_close.py', 'test_only_the_terminal_settle_may_close_on_expiry'),
    # bcs/tests/test_quote_batching.py
    ('bcs/tests/test_quote_batching.py', 'test_the_poll_loop_actually_calls_the_prefetch'),
    ('bcs/tests/test_quote_batching.py', 'test_the_order_path_asks_the_broker_on_every_depth_wait'),
    # bcs/tests/test_replay_feb2026_icici.py
    ('bcs/tests/test_replay_feb2026_icici.py', 'test_the_negative_spread_guard_alone_would_also_have_refused_it'),
    # bcs/tests/test_replay_jul2026_nhpc.py
    ('bcs/tests/test_replay_jul2026_nhpc.py', 'test_the_corroboration_veto_is_recorded_even_when_it_does_not_alert'),
    # bcs/tests/test_s3_reconcile_residue.py
    ('bcs/tests/test_s3_reconcile_residue.py', 'test_the_poll_loop_calls_the_residue_sweep'),
    ('bcs/tests/test_s3_reconcile_residue.py', 'test_legs_of_covers_every_option_leg_field_any_store_declares'),
    ('bcs/tests/test_s3_reconcile_residue.py', 'test_the_post_close_audit_uses_that_one_reader'),
    # bcs/tests/test_time_stop_retry.py
    ('bcs/tests/test_time_stop_retry.py', 'test_the_retry_is_wired_into_the_cron_loop'),
    ('bcs/tests/test_time_stop_retry.py', 'test_the_time_stop_still_has_one_due_call_site'),
    ('bcs/tests/test_time_stop_retry.py', 'test_the_force_close_clock_has_one_definition'),
    # bcs/tests/test_tp_latch.py
    ('bcs/tests/test_tp_latch.py', 'test_the_shared_decision_is_the_one_both_engines_call'),
    # bcs/tests/test_zebra_bridge.py
    ('bcs/tests/test_zebra_bridge.py', 'test_every_per_position_state_dict_is_keyed_by_trade_key'),
    ('bcs/tests/test_zebra_bridge.py', 'test_the_LIST_COMMAND_actually_calls_it'),
    # zebra/tests/test_auto_entry.py
    ('zebra/tests/test_auto_entry.py', 'test_paper_mode_never_reaches_the_live_entry_path'),
    # zebra/tests/test_bcs_gates.py
    ('zebra/tests/test_bcs_gates.py', 'test_live_alerts_still_escape_interpolated_tags'),
    # zebra/tests/test_capital.py
    ('zebra/tests/test_capital.py', 'test_verification_never_orders'),
    ('zebra/tests/test_capital.py', 'test_compounding_is_read_strictly'),
    ('zebra/tests/test_capital.py', 'test_the_two_default_sources_do_not_drift_further'),
    ('zebra/tests/test_capital.py', 'test_max_dte_is_45_in_BOTH_sources'),
    ('zebra/tests/test_capital.py', 'test_the_vetting_master_switch_is_in_the_TRACKED_config'),
    ('zebra/tests/test_capital.py', 'test_the_capital_keys_agree_across_both_sources'),
    ('zebra/tests/test_capital.py', 'test_both_entry_builders_read_the_lot_count_from_the_record'),
    # zebra/tests/test_cli_block.py
    ('zebra/tests/test_cli_block.py', 'test_no_spawn_is_burned_into_a_known_block'),
    ('zebra/tests/test_cli_block.py', 'test_the_drop_message_states_attempts_that_actually_ran'),
    ('zebra/tests/test_cli_block.py', 'test_the_out_of_band_drain_is_wired_into_check_watching'),
    # zebra/tests/test_decisions.py
    ('zebra/tests/test_decisions.py', 'test_record_assigns_ids_and_persists'),
    ('zebra/tests/test_decisions.py', 'test_failed_record_leaves_no_phantom_row'),
    ('zebra/tests/test_decisions.py', 'test_concurrent_writers_lose_no_decisions'),
    # zebra/tests/test_digest.py
    ('zebra/tests/test_digest.py', 'test_the_running_list_is_idempotent_and_keeps_human_ticks'),
    ('zebra/tests/test_digest.py', 'test_the_digest_never_writes_to_the_trade_store'),
    ('zebra/tests/test_digest.py', 'test_it_is_wired_as_a_cli_verb'),
    ('zebra/tests/test_digest.py', 'test_the_real_store_as_of_08_26_matches_what_that_day_reported'),
    # zebra/tests/test_digest_tp_latch.py
    ('zebra/tests/test_digest_tp_latch.py', 'test_the_digest_no_longer_carries_its_own_copy_of_the_vocabulary'),
    # zebra/tests/test_engine_log.py
    ('zebra/tests/test_engine_log.py', 'test_the_catalogue_still_matches_what_the_engine_writes'),
    ('zebra/tests/test_engine_log.py', 'test_the_digest_still_writes_nothing_to_any_store'),
    # zebra/tests/test_exit_engine_heartbeat.py
    ('zebra/tests/test_exit_engine_heartbeat.py', 'test_the_peer_reads_the_stand_down_switch_ONLY_to_report_it'),
    ('zebra/tests/test_exit_engine_heartbeat.py', 'test_the_arming_preflight_places_no_order'),
    ('zebra/tests/test_exit_engine_heartbeat.py', 'test_the_dedup_survives_the_process'),
    ('zebra/tests/test_exit_engine_heartbeat.py', 'test_the_counts_come_from_the_loaded_book'),
    # zebra/tests/test_exit_vocabulary.py
    ('zebra/tests/test_exit_vocabulary.py', 'test_the_real_cohort_leaves_the_gate_unmet'),
    # zebra/tests/test_exits_external.py
    ('zebra/tests/test_exits_external.py', 'test_the_constant_itself_is_built_by_strict_bool'),
    # zebra/tests/test_fees.py
    ('zebra/tests/test_fees.py', 'test_the_live_entry_path_hands_over_its_leg_book'),
    # zebra/tests/test_history.py
    ('zebra/tests/test_history.py', 'test_attraction_is_wired_into_the_vet_context'),
    ('zebra/tests/test_history.py', 'test_the_agent_is_told_the_rates_moved'),
    ('zebra/tests/test_history.py', 'test_the_agent_is_told_a_missing_monthly_section_is_deliberate'),
    ('zebra/tests/test_history.py', 'test_the_agent_is_told_how_to_read_the_new_evidence'),
    ('zebra/tests/test_history.py', 'test_the_agent_is_warned_that_a_fast_rate_is_not_a_green_light'),
    ('zebra/tests/test_history.py', 'test_velocity_is_wired_into_the_vet_context'),
    ('zebra/tests/test_history.py', 'test_the_agent_is_told_what_in_progress_means'),
    ('zebra/tests/test_history.py', 'test_the_agent_actually_receives_this_document'),
    # zebra/tests/test_live_mode.py
    ('zebra/tests/test_live_mode.py', 'test_silencing_an_alert_does_not_silence_the_EXIT'),
    # zebra/tests/test_m2_entry_budget.py
    ('zebra/tests/test_m2_entry_budget.py', 'test_the_budget_is_checked_BEFORE_the_arming_switch'),
    ('zebra/tests/test_m2_entry_budget.py', 'test_the_phase_is_armed_around_check_watching_and_always_disarmed'),
    ('zebra/tests/test_m2_entry_budget.py', 'test_the_budget_never_interrupts_an_entry_in_flight'),
    ('zebra/tests/test_m2_entry_budget.py', 'test_the_budget_does_not_execute_in_paper_mode'),
    # zebra/tests/test_m3_options_csv_staleness.py
    ('zebra/tests/test_m3_options_csv_staleness.py', 'test_no_exit_path_consults_the_chain_freshness'),
    ('zebra/tests/test_m3_options_csv_staleness.py', 'test_the_cycle_calls_it_and_cannot_be_killed_by_it'),
    ('zebra/tests/test_m3_options_csv_staleness.py', 'test_the_tracked_default_and_the_code_default_agree'),
    # zebra/tests/test_m6_unavailable_asymmetry.py
    ('zebra/tests/test_m6_unavailable_asymmetry.py', 'test_unavailable_is_not_on_the_entry_allowlist'),
    ('zebra/tests/test_m6_unavailable_asymmetry.py', 'test_the_live_dedup_check_agrees_with_the_gate'),
    ('zebra/tests/test_m6_unavailable_asymmetry.py', 'test_the_exit_gate_still_PROCEEDS_on_unavailable'),
    ('zebra/tests/test_m6_unavailable_asymmetry.py', 'test_the_asymmetry_is_written_down_where_it_is_decided'),
    # zebra/tests/test_manual_entry_ownership.py
    ('zebra/tests/test_manual_entry_ownership.py', 'test_the_cli_decides_ownership_BEFORE_it_writes_the_record'),
    ('zebra/tests/test_manual_entry_ownership.py', 'test_the_cli_reads_the_broker_ONCE'),
    ('zebra/tests/test_manual_entry_ownership.py', 'test_the_ownership_check_reads_the_RESOLVED_symbols'),
    # zebra/tests/test_mfe.py
    ('zebra/tests/test_mfe.py', 'test_every_exit_kind_the_monitor_raises_can_record_a_verdict'),
    # zebra/tests/test_n11_debit_sl_study_vocabulary.py
    ('zebra/tests/test_n11_debit_sl_study_vocabulary.py', 'test_the_study_reports_reasons_no_reader_understands'),
    ('zebra/tests/test_n11_debit_sl_study_vocabulary.py', 'test_the_fix_changes_nothing_on_the_current_book'),
    ('zebra/tests/test_n11_debit_sl_study_vocabulary.py', 'test_every_reason_in_the_live_book_is_recognised'),
    # zebra/tests/test_postmortem.py
    ('zebra/tests/test_postmortem.py', 'test_the_batch_is_wired_into_the_cycle'),
    # zebra/tests/test_quote_verb.py
    ('zebra/tests/test_quote_verb.py', 'test_vetting_doc_names_the_verb_for_both_channels'),
    # zebra/tests/test_rate_limit_diagnosis.py
    ('zebra/tests/test_rate_limit_diagnosis.py', 'test_exit_monitoring_runs_before_the_discretionary_scanner'),
    ('zebra/tests/test_rate_limit_diagnosis.py', 'test_the_analyzer_costs_one_quote_not_nine'),
    ('zebra/tests/test_rate_limit_diagnosis.py', 'test_fetched_candles_are_cached_without_todays_partial_bar'),
    ('zebra/tests/test_rate_limit_diagnosis.py', 'test_the_throttle_is_wired_into_every_historical_call'),
    # zebra/tests/test_report_exit_vocabulary.py
    ('zebra/tests/test_report_exit_vocabulary.py', 'test_the_report_no_longer_carries_its_own_copy_of_the_vocabulary'),
    # zebra/tests/test_review_2026_08_13.py
    ('zebra/tests/test_review_2026_08_13.py', 'test_a_per_channel_deny_is_NOT_in_the_settings_backstop'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_settings_backstop_matches_the_spawn_deny_list'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_quarantine_leaves_a_marker_for_the_monitor_to_alert'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_retired_ticket_raises_instead_of_telegramming'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_back_ratio_has_no_entry_path_left'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_a_spawn_that_never_reports_a_pid_frees_its_slot'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_funds_check_is_wired_into_the_path_that_actually_trades'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_a_budget_refusal_does_not_claim_the_cli_is_broken'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_unbudgeted_token_never_touches_the_budget_file'),
    ('zebra/tests/test_review_2026_08_13.py', 'test_the_budget_file_is_written_atomically'),
    # zebra/tests/test_review_2026_08_29.py
    ('zebra/tests/test_review_2026_08_29.py', 'test_a_capital_refusal_in_paper_does_not_skip_the_vet'),
    ('zebra/tests/test_review_2026_08_29.py', 'test_the_cap_still_refuses_when_it_is_LIVE'),
    ('zebra/tests/test_review_2026_08_29.py', 'test_the_adapter_says_the_time_stop_is_read_LIVE_not_frozen'),
    ('zebra/tests/test_review_2026_08_29.py', 'test_a_hand_entry_is_always_already_filled'),
    ('zebra/tests/test_review_2026_08_29.py', 'test_the_capital_check_still_runs_on_a_hand_entry'),
    # zebra/tests/test_review_tail.py
    ('zebra/tests/test_review_tail.py', 'test_the_liquidity_check_follows_the_configured_oi'),
    ('zebra/tests/test_review_tail.py', 'test_paper_retries_a_triggered_signal_that_never_entered'),
    ('zebra/tests/test_review_tail.py', 'test_live_still_stops_at_the_ticket'),
    ('zebra/tests/test_review_tail.py', 'test_the_cycle_is_delimited_and_timed'),
    ('zebra/tests/test_review_tail.py', 'test_the_expiry_nag_survives_a_dark_book'),
    ('zebra/tests/test_review_tail.py', 'test_the_breakeven_guard_is_actually_wired_into_the_entry'),
    # zebra/tests/test_scorecard_fees.py
    ('zebra/tests/test_scorecard_fees.py', 'test_the_scorecard_holds_no_rate_of_its_own'),
    # zebra/tests/test_second_source.py
    ('zebra/tests/test_second_source.py', 'test_the_veto_holds_the_value_triggers_not_the_spot_ones'),
    # zebra/tests/test_side_channels.py
    ('zebra/tests/test_side_channels.py', 'test_every_exit_branch_routes_through_the_release_helper'),
    # zebra/tests/test_store_locking.py
    ('zebra/tests/test_store_locking.py', 'test_dedup_error_releases_lock_and_leaves_disk_clean'),
    ('zebra/tests/test_store_locking.py', 'test_partial_mutation_is_rolled_back_on_exception'),
    ('zebra/tests/test_store_locking.py', 'test_drive_sync_never_clobbers_concurrent_writes'),
    # zebra/tests/test_tp_latch.py
    ('zebra/tests/test_tp_latch.py', 'test_only_the_take_profit_reads_the_latch_in_the_cascade'),
    ('zebra/tests/test_tp_latch.py', 'test_nothing_ever_CLEARS_the_latch'),
    # zebra/tests/test_vet_cli.py
    ('zebra/tests/test_vet_cli.py', 'test_each_spawn_gets_its_own_transcript'),
    # common/tests/test_layered_config.py
    ('common/tests/test_layered_config.py', 'test_no_tracked_defaults_file_contains_a_secret'),
    ('common/tests/test_layered_config.py', 'test_defaults_and_overlay_do_not_both_claim_the_same_leaf'),
    # common/tests/test_nse_holidays.py
}


def _guards():
    """Every source-reading test in the suite, as (file, name, docstring)."""
    out = []
    for r in ROOTS:
        for f in sorted((HELPER / r).glob('test_*.py')):
            key = f.relative_to(HELPER).as_posix()
            src = io.open(f, encoding='utf-8').read()
            try:
                tree = ast.parse(src)
            except SyntaxError:                # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if not node.name.startswith('test_'):
                    continue
                seg = ast.get_source_segment(src, node) or ''
                if any(k in seg for k in MARKERS):
                    out.append((key, node.name, ast.get_docstring(node) or ''))
    return out


def test_a_new_source_guard_names_what_retires_it():
    """THE RULE. Everything above is the argument for it.

    A guard that reads source freezes the file layout, and it is permanent by
    default: nobody downstream can tell whether it still matters, because the
    reason lives in the head of whoever wrote it. Writing the retirement
    condition down costs one sentence at the moment the author knows it.
    """
    undeclared = [
        (f, name) for f, name, doc in _guards()
        if (f, name) not in BASELINE and MARKER_PHRASE not in doc
    ]
    assert not undeclared, (
        'these source-reading guards are new and do not say what retires '
        'them. Add a "%s <the structural change that makes this guard '
        'unnecessary>" line to each docstring:\n%s'
        % (MARKER_PHRASE,
           '\n'.join('  %s::%s' % fn for fn in sorted(undeclared))))


def test_the_baseline_can_only_shrink():
    """A stale entry is a guard that was deleted or annotated — both are the
    debt being PAID, so the baseline must not keep naming it.

    Without this the list would silently accumulate names of tests that no
    longer exist, and the count would stop meaning anything — which is exactly
    the failure mode of the guards it is regulating.
    """
    live = {(f, name) for f, name, _doc in _guards()}
    annotated = {(f, name) for f, name, doc in _guards()
                 if MARKER_PHRASE in doc}
    stale = sorted((BASELINE & annotated) | (BASELINE - live))
    assert not stale, (
        'these baseline entries are no longer undeclared guards — remove them '
        'from BASELINE, which may only ever shrink:\n%s'
        % '\n'.join('  %s::%s' % fn for fn in stale))


def test_the_detector_actually_detects():
    """The negative control, and the one this file cannot do without.

    Every assertion above passes trivially if `_guards()` returns nothing —
    a typo in MARKERS, a moved test directory, a renamed root. Then the policy
    reads as enforced and enforces nothing, which is the shape it exists to
    prevent one level down.
    """
    guards = _guards()
    assert len(guards) > 100, (
        'the source-guard detector found only %d guards; it found 179 on '
        '2026-08-30, so it is probably looking in the wrong place'
        % len(guards))
    files = {f for f, _n, _d in guards}
    assert len(files) > 30


def test_the_new_guards_from_this_pass_declare_themselves():
    """Leading by example, and proving the mechanism works on real cases.

    A policy whose first compliant instance is hypothetical is a policy nobody
    has tried to follow.
    """
    declared = {f for f, _n, doc in _guards() if MARKER_PHRASE in doc}
    assert declared, 'no guard anywhere declares a retirement condition'


@pytest.mark.parametrize('path', sorted({f for f, _n, _d in _guards()}))
def test_every_guard_file_is_readable(path):
    """Cheap, and it pins the paths the policy is computed over: a root that
    silently stops matching would empty the inventory."""
    assert (HELPER / path).exists()
