# Monitoring semantics

This document is the normative V1 contract for interpreting probe evidence.
Storage, APIs, statistics, exports, and the UI must use the same rules. Status
labels describe observations and must not claim that an ISP, router, power
source, or other party caused a failure.

## 1. Terms

- **Probe:** one check against one configured target.
- **Round:** the bounded set of probes scheduled for one sampling time.
- **Round status:** the immediate classification of one completed round.
- **Stable status:** the confirmed status after hysteresis is applied.
- **Incident:** a confirmed, bounded period of one non-online stable status.
- **Monitoring gap:** a period for which the monitor has no trustworthy
  connectivity classification.
- **Unknown:** insufficient or untrustworthy evidence. Unknown is never treated
  as online or offline.

All persisted timestamps use UTC. The UI may render them in the configured site
timezone and must include the UTC offset where ambiguity is possible.

## 2. Probe outcomes

Every attempted probe produces one of these outcomes:

- `SUCCESS`: the configured expectation was met.
- `FAILURE`: the probe ran through the intended route and did not meet its
  expectation before its timeout.
- `UNKNOWN`: the probe did not provide trustworthy reachability evidence. This
  includes cancellation, invalid configuration, an ambiguous or prohibited
  route, clock uncertainty, and internal monitor errors.
- `NOT_APPLICABLE`: the probe or component is disabled.

A failure records a bounded error class such as `timeout`, `dns`, `network`,
`tls`, `unexpected_response`, `route_policy`, or `internal`. Diagnostic text
must be sanitized and size-limited before persistence.

Latency is recorded only when meaningful. A timeout is not stored as if it
were a measured latency.

## 3. Component evidence

Probe outcomes are normalized into four component results:

- `REACHABLE`
- `UNREACHABLE`
- `UNKNOWN`
- `NOT_APPLICABLE`

### Gateway

The gateway component tests the detected or explicitly configured default-route
gateway. A successful check means `REACHABLE`; a failed check means
`UNREACHABLE`. Failure means only that the gateway did not answer the selected
probe. It does not prove router hardware failure.

### External IP

The external-IP component uses at least two independently operated targets by
default.

- At least one successful target means `REACHABLE`.
- `UNREACHABLE` requires all enabled targets to have been attempted through an
  accepted native-WAN route and to have failed.
- Fewer than the configured minimum trustworthy attempts means `UNKNOWN`.

A single failed ICMP probe can never establish a general internet outage.

### DNS

The primary DNS component resolves a configured stable hostname through the
system resolver. The result records which resolver path was exercised.

- A valid, expected answer means `REACHABLE`.
- A trustworthy resolution failure means `UNREACHABLE`.
- A cached-only answer must not be used as sole proof of current DNS health.
- An unidentifiable resolver path or invalid route means `UNKNOWN`.

An optional direct query to a configured external resolver is diagnostic
evidence and must remain distinguishable from the system-resolver result.

### HTTPS

The HTTPS component performs a small request with TLS validation and validates
the configured response expectation.

- At least one successful endpoint means `REACHABLE`.
- `UNREACHABLE` requires every enabled, trustworthy attempt to fail.
- No trustworthy attempt means `UNKNOWN`.

HTTPS and external-IP evidence are complementary. HTTPS alone may depend on
DNS, while ICMP may be filtered or deprioritized.

## 4. Route policy

External probes must use the installation's native default WAN route. Before a
probe can contribute `FAILURE` evidence, its selected route and interface must
satisfy the configured route policy.

- A known VPN interface or prohibited route produces `UNKNOWN`, not `FAILURE`.
- A route change is diagnostic metadata and triggers revalidation.
- If the intended route cannot be established, the monitor reports
  `MONITORING_UNKNOWN` rather than claiming an outage.
- A future VPN-specific probe must be a separate category and cannot contribute
  to native-WAN classification.

## 5. Round classification

V1 stable statuses are:

- `ONLINE`
- `INTERNET_DOWN`
- `GATEWAY_UNREACHABLE`
- `DNS_FAILURE`
- `PARTIAL_CONNECTIVITY`
- `MONITORING_UNKNOWN`

The following table defines the primary cases. `R`, `U`, and `?` mean
`REACHABLE`, `UNREACHABLE`, and `UNKNOWN`. Disabled optional evidence is omitted.

| Gateway | External IP | DNS | HTTPS | Round status | Reason |
|---|---|---|---|---|---|
| R | R | R | R | `ONLINE` | All required components are healthy. |
| U | U or ? | any | U or ? | `GATEWAY_UNREACHABLE` | The gateway fails, no external signal succeeds, and at least one external group also fails. |
| U | R | any | any | `PARTIAL_CONNECTIVITY` | External-IP success contradicts the failed gateway probe. |
| U | U or ? | any | R | `PARTIAL_CONNECTIVITY` | HTTPS success contradicts the failed gateway probe. |
| R | U | any | U | `INTERNET_DOWN` | Independent IP and HTTPS evidence both fail while the gateway responds. |
| R | R | U | any | `DNS_FAILURE` | External IP works while the system resolver fails. |
| R | U | R | R | `PARTIAL_CONNECTIVITY` | HTTPS works despite failed external ICMP targets. |
| R | R | R | U | `PARTIAL_CONNECTIVITY` | IP and DNS work, but configured HTTPS endpoints fail. |
| R | R | ? | R | `PARTIAL_CONNECTIVITY` | Internet works, but required DNS evidence is incomplete. |
| any | ? | ? | ? | `MONITORING_UNKNOWN` | There is not enough trustworthy evidence to classify connectivity. |

Rules not represented by an exact row follow these priorities:

1. Invalid timing, stale monitoring, or prohibited routing yields
   `MONITORING_UNKNOWN`.
2. `GATEWAY_UNREACHABLE` requires a failed gateway check, no successful
   external evidence, and at least one trustworthy failed external group.
3. `INTERNET_DOWN` requires a reachable gateway plus both failed external-IP
   and failed HTTPS groups.
4. `DNS_FAILURE` requires successful external-IP evidence plus failed system
   DNS. An HTTPS failure caused by DNS does not promote this to internet down.
5. Contradictory trustworthy evidence yields `PARTIAL_CONNECTIVITY`.
6. `ONLINE` requires all enabled required components to be healthy.
7. Any remaining case with insufficient evidence yields
   `MONITORING_UNKNOWN`.

Detailed component results are always retained even when the primary status
hides them through precedence.

## 6. Confirmation and recovery

Default timing is configurable:

- Round interval: 10 seconds.
- Per-probe timeout: 3 seconds or less.
- Failure confirmation: 3 consecutive non-online rounds.
- Recovery confirmation: 2 consecutive `ONLINE` rounds.
- Category-transition confirmation: 3 consecutive non-online rounds that no
  longer support the current confirmed category.

Rounds must not accumulate. If a round is still running when the next one is
due, the scheduler records overrun evidence and does not launch an overlapping
round.

If all rounds in a confirming run support the same specific category, that
category is confirmed. If the run contains different non-online categories,
the confirmed status is `PARTIAL_CONNECTIVITY` unless one more-specific status
is supported by every round. This prevents continuously impaired but changing
evidence from evading confirmation.

`MONITORING_UNKNOWN` is a safety state rather than an outage candidate. It is
shown as soon as a completed round cannot be classified, or when the monitor
crosses its staleness threshold; outage hysteresis does not delay it.

When a failure is confirmed:

- `observed_start` is the timestamp of the first round in the confirming run.
- `confirmed_start` is the timestamp of the threshold-crossing round.
- Exactly one incident is opened idempotently.

When recovery is confirmed:

- `observed_end` is the timestamp of the first `ONLINE` round in the confirming
  recovery run.
- `confirmed_end` is the timestamp of the threshold-crossing `ONLINE` round.
- Incident duration is `observed_end - observed_start`.

If failure returns before recovery confirms, the tentative recovery is
discarded and the same incident remains open. A brief run that never reaches a
confirmation threshold remains available as probe evidence but does not become
a confirmed incident.

When one confirmed failure category changes to another, the new evidence must
meet the transition threshold. Consistent rounds confirm the specific new
category; mixed non-online rounds confirm `PARTIAL_CONNECTIVITY`. The old
incident then ends and the new incident begins at the first observation of the
confirming run. The transition and both evidence sets are retained; an incident
is never silently reclassified.

Until a failure or transition confirms, the last stable status remains in the
historical status interval. The live API may additionally expose the pending
candidate and its current count so the UI can show "Checking" without claiming
a confirmed incident. Once confirmed, the new interval is backdated to
`observed_start`.

## 7. Process restarts and monitoring gaps

The monitor persists a boot identifier, process identifier, heartbeat, and last
completed round. On startup it compares these with the new process and host
state.

- A host boot-ID change identifies a host reboot.
- An unchanged boot ID with a new monitor process identifies a process restart.
- Neither condition proves a power failure.
- A stale heartbeat without a process transition still creates a gap when the
  configured staleness threshold is exceeded.

A gap begins after the last trustworthy pre-gap observation and ends at the
first trustworthy post-gap observation. Its reason is evidence, not an asserted
root cause.

An incident open before a gap is ended as `INTERRUPTED` at the gap boundary,
with no recovery timestamp. After the gap, classification starts fresh. A
post-gap failure can create a new linked incident after normal confirmation,
but the system must not claim that the failure continued through the gap.

`MONITORING_GAP` is an interval/event category. During that interval, the
primary connectivity status is `MONITORING_UNKNOWN`.

## 8. Clock handling

Wall-clock UTC timestamps are persisted for correlation and display. Monotonic
time is used for in-process elapsed durations and scheduler decisions.

The monitor compares wall-clock and monotonic deltas. A difference beyond the
configured tolerance marks the affected interval uncertain. Samples collected
before the system clock is considered synchronized may be retained for
diagnostics, but they cannot contribute trusted availability intervals.

Clock-uncertain time is represented as `MONITORING_UNKNOWN`; timestamps are
never silently rewritten to invent continuity.

## 9. Availability and coverage

Calculations use half-open UTC intervals `[start, end)` clipped to the selected
reporting window. Local time and daylight-saving changes affect presentation,
not duration arithmetic.

For a reporting window:

```text
classified = online + internet_down + gateway_unreachable
             + dns_failure + partial_connectivity

fully_online_availability = online / classified
coverage = classified / reporting_window
unknown = reporting_window - classified
```

Policy decisions for V1:

- Only `ONLINE` contributes to the fully-online numerator.
- `DNS_FAILURE` and `PARTIAL_CONNECTIVITY` are impairments, not confirmed
  general internet outages.
- `INTERNET_DOWN` is counted as a confirmed general internet outage.
- `GATEWAY_UNREACHABLE` is counted and displayed separately as a local-path
  outage observation.
- Unknown and gap time contributes to neither reachable nor unreachable time.
- The UI reports status-duration breakdowns alongside the headline percentage.
- If `classified` is zero, availability is `null` / "No observed data", never
  zero or 100 percent.

Open intervals are clipped at the query's effective end time and displayed as
ongoing. Pagination cannot affect totals or exports.

## 10. Required invariants

Implementations and tests must preserve these invariants:

1. One failed target cannot confirm `INTERNET_DOWN`.
2. Failed DNS with successful external IP cannot become `INTERNET_DOWN`.
3. Contradictory successful and failed evidence cannot become a confident
   outage.
4. Unknown time cannot count as online or offline.
5. Confirmation delay does not shorten the observed incident duration.
6. Recovery requires consecutive healthy rounds.
7. A restart cannot duplicate an already-opened incident.
8. A monitoring gap cannot invent either continued failure or recovery.
9. Status intervals do not overlap for one site.
10. Reprocessing the same completed round is idempotent.

These rules will be encoded as table-driven and sequence-based tests before
live probes or the web application are implemented.
