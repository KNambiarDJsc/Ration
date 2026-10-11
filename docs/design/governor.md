# Context Governor: design

Status: first slice implemented (`ration governor`, status-line price tag, boundary nudge). Evidence below comes from one heavy-use Windows machine; see [Limits](#limits-and-what-is-unproven).

## 1. The problem

Long Claude Code sessions are expensive in a way no per-turn view shows. **Every call re-reads the whole context** at the cache-read rate, so the cost of a message is proportional to how large the context is *when you send it*.

Measured on one machine over 30 days (cache-weighted tokens: input 1, output 5, cache write 1.25, cache read 0.1):

| Fact | Value |
|---|---|
| Share of usage that was cache reads of carried context | 79% |
| Top session's share of all usage | 62% (3,435 calls, context up to 933k) |
| Claude Code's default compaction point on native-1M models | ~967k tokens |
| Cost of one message at 900k context vs 90k | ~10x (≈0.5% vs ≈0.05% of a 5-hour window, calibrated) |
| Context size right after a compaction (12 events, any prior size) | 65k–125k, median ~85k |

The last row is the lever. Compaction returns the context to roughly the same size whether it happened at 220k or 930k, so *waiting longer to compact only buys more expensive messages*.

## 2. What Shunt does, and why it isn't this

[Shunt](https://github.com/spotify/portal-ai-plugins/tree/main/plugins/shunt) (Spotify) hard-blocks reads of files over 350 lines and sends them to a cheaper model through an internal Portal/AiKA service; Claude gets that model's summary instead. It targets *tool-result size*.

We measured that target on the same data: tool results kept in context are ~9% of usage, and shaping every result over 2k tokens to ~1k would save **~2%**. Shunt also needs infrastructure few can use, returns lossy summaries Claude can't verify, and doesn't measure net savings. We keep its good idea (keep bulk work out of the main context) as a later, optional component (section 7), but it is not the lever on this workload.

## 3. Cost model

Per call, with `ctx` the prompt size and `g` the tokens added since the last call:

```
cost(call) = 0.1·ctx  +  1.25·g  +  5·output          # warm cache
           = 1.25·ctx + ...                           # cache expired (idle > TTL)
compaction (ctx ≥ W) = 0.1·ctx + 5·S + 1.25·B ; then ctx = B + R
```

`W` is the auto-compact window, `B` the post-compaction size (measured), `S` the summary length, `R` tokens re-fetched afterwards (files read again). Growth `g` and output are **replayed from the real transcript**; only the policy `W` changes. The model reproduces your actual usage to within ~1–12% when run at your current window, which is the check that makes the what-ifs meaningful.

Why a sensible window is *not* "as small as possible": if `B + R` approaches `W`, Claude compacts almost every call and cost explodes (we see 4x–6x in simulation). The governor therefore requires a window to be **non-thrashing and still worthwhile under pessimistic parameters**, and picks the best *worst case*, preferring the larger window on ties.

Every candidate is scored under four assumption sets:

| Set | Post-compaction size | Re-fetched after each compaction | Compaction request reads the conversation at |
|---|---|---|---|
| optimistic | 0.8 x measured | 25k | cache-read rate (0.1) |
| measured | measured | 25k | fresh-input rate (1.0), as measured live |
| heavy re-fetch | measured | 75k | 1.0 |
| pessimistic | 1.5 x measured | 100k | full rebuild rate (1.25) |

On the reference machine 400k saves 35% (measured) and 26% (pessimistic); 250k saves 45% measured but *loses* 9% in the pessimistic set, so it is rejected.

### Live verification (2026-10-11, Claude Code 2.1.296)

| Question | Test | Result |
|---|---|---|
| Does the setting take effect? | `ration governor apply`, then `/autocompact` | "400k tokens (from settings)" |
| What is `auto`? | `--autocompact auto` + `/autocompact` per model | 1M for Haiku 5.5, Sonnet 5.5, Opus 5.5 |
| Does compaction fire at the window? | haiku, `--autocompact 100000`, five ~20k-token reads | `compact_boundary` with `trigger: auto`, 202,785 → 53,087 tokens |
| Where does context land afterwards? | first call after that compaction | 120,580 tokens, *above* the 100k window (recent messages are preserved) |
| How is the compaction request billed? | session `modelUsage` minus the per-call usage in the transcript | ~117.7k fresh input + 37k cache read + 2.5k output; **not in the transcript** |
| How long does a compaction take? | `compactMetadata.durationMs` in real sessions | 13 s at 200k; 130–224 s at 400k–935k |

Consequences: the "measured" set prices the compaction request at 1.0 per token; the thrash guard is justified by an observed case, not a hypothetical; and the model check compares against transcript totals with the compaction request left out, because transcripts don't contain it.

Claude Code writes a `compact_boundary` system record for every compaction (`trigger`, `preTokens`, `postTokens`, `durationMs`). The governor currently infers compactions from context drops; reading these records directly is the obvious next refinement, and would also separate automatic from manual compactions.

The replay must be compared against the window the history was actually recorded under. After `apply`, the report replays only the history from before the change, at the previous window; otherwise the model check is meaningless (this was a real bug, caught the first time the setting was applied).

## 4. System

```
 Meter ───────────► Model ───────────► Controller ───────────► Verifier
 transcripts        replay + learn     one official setting    realized before/after
 (tokens, model,    B, growth, TTL     autoCompactWindow       per-call cost, compactions
 timestamps)        pick robust W      + in-session nudges     auto-suggest backoff
        ▲                                     │                       │
        └─────────────── calibration ◄────────┴───────────────────────┘
                 (usage % per token, learned on this machine)
```

| Part | Where | What it does |
|---|---|---|
| Meter | `transcript.py`, status line, hooks | Exact tokens per call from session files; usage % from the official status line; `rate_limit_event` |
| Model | `governor.py` | Replays real sessions under candidate windows across four assumption sets; flags thrash; reports model error |
| Controller | `integrations/claude_install.py`, `cli.py` | Writes/removes the top-level `autoCompactWindow` (backed up, reversible, warns about env/per-model precedence). In-session: status-line price tag; "good moment to compact" at green-test boundaries |
| Verifier | `governor realized` / `ration governor status` | Compares per-call cost, average context and compactions/100 calls before vs after applying |

Surfaces:

- `ration governor`: backtest table, recommendation, trade-off, model error.
- `ration governor apply [--window N]` / `revert` / `status`.
- Status line: `ctx 312k · msg ≈0.2%` once context passes 150k (price of the *next* message).
- Stop hook: at a natural boundary (files changed, tests green) above 250k context, once per 30 min.

## 5. Safety properties

- **Opt-in, reversible.** `apply` backs up `settings.json` first; `revert` restores the previous value; `/autocompact auto` also works from inside Claude Code.
- **Official controls only.** The setting is documented (`autoCompactWindow`, `/autocompact`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, 100k–1M). No credentials, no private endpoints, no hook that edits the conversation.
- **Never recommends what it can't justify.** No history, small contexts, or <15% worst-case saving all yield "no change recommended".
- **Fails soft.** Low model fit is reported as low confidence; thrash windows are shown as such, never suggested.
- **Local only.** Reads token counts, timestamps and model names; message text is not read for this feature.

## 6. Evaluation plan

1. **Shadow (done on one machine):** replay accuracy vs real usage (1–12% error), sensitivity across assumption sets.
2. **Live (next):** after `apply`, `governor status` tracks per-call cost, average context, compactions/100 calls. Success: per-call cost down, compactions below ~1 per 12 calls.
3. **Quality guard (not built):** track re-reads of files read before a compaction and stuck score before/after; auto-suggest a higher window if either worsens.
4. **Cross-machine:** the parameters (`B`, growth, call rate) should be gathered from more than one user before the saving range is quoted publicly.

## 7. Roadmap

| Phase | What | Expected value (measured basis) |
|---|---|---|
| Now | Window recommendation, price tag, boundary nudge, verifier | 26–35% of usage for long-session users (this machine; mechanics verified live, the saving itself still a replay) |
| Next | Quality guard + automatic backoff; per-model windows; per-project windows | Safety |
| Next | Idle-gap policy: at cache expiry, fresh-start or compact *before* paying the rebuild | Part of the ~8% idle rebuild share |
| Later | Delegation gate on native subagents (Shunt's idea, no external service): route bulk reads/searches to a cheap-model subagent, return a pointer + summary | ~2% on this workload; higher for 200k-window users (to be measured) |
| Later | Output shaping (PreToolUse `updatedInput` wrapping noisy commands, spill to a file) | ~0.4% measured; mostly a latency win |

## 8. Limits and what is unproven

- **One machine, one heavy user.** 62% of its usage was a single session. The saving range will be smaller for people who already `/clear` often or use 200k-window models (where the default is already near-optimal; the tool then says "no change").
- **`B` is measured from few events** (12 compactions across two sessions, 65k–125k, plus one live test at 120k). Below 3 observed compactions the governor uses 85k and says so.
- **Compaction requests are not in transcripts**, so everything RATION derives from transcripts (per-turn tokens, `xray` shares, the "real usage" side of the model check) leaves them out. The replay adds them back at the live-measured rate.
- **Calibration is thin** (a handful of turns): the % price tags are approximate until more turns are observed.
- **Quality is not measured.** Compacting earlier summarizes older detail sooner. The model counts re-fetch as a token cost (R) but not as lower answer quality. This is why the recommendation favors larger windows on ties, and why handoff exists for lossless restarts.
- **Subscription limits may weight tokens differently** from the published price ratios the cost model uses. Calibration on the 5-hour meter agrees in direction; absolute % per message should be read as an estimate.
- **Anthropic recommends the opposite default.** On Claude Code 2.1.296, `/autocompact` reports `auto` as 1M tokens for Haiku 5.5, Sonnet 5.5 and Opus 5.5 and states that auto "is strongly recommended for the best cost and performance" and that overriding it "may result in high token usage, especially when resuming long sessions". They have fleet-wide data; this design has one machine. The disagreement is real and unresolved: the replay says a lower window is cheaper for long sessions even when the compaction request is priced as a full rebuild, but only live before/after numbers (`ration governor status`) can settle it for a given user. Known one-off cost: the first message into an existing session larger than the new window triggers one large compaction.
- **Claude Code already flags long context** in `/usage` at ≥10% of usage. The governor's contribution is the quantified, backtested, robustly chosen setting plus verification, not the observation itself.
