# ADR-009: Every Image This Cluster Runs Is Pinned Explicitly

**Date:** 2026-09-08
**Status:** Proposed
**Related:** [ADR-008](0008-storage-mechanisms.md)

## Context

Container versions here are decided by whichever Helm chart deploys them. A chart ships an
`appVersion`, its templates render that as the image tag, and Renovate bumps the chart. Nothing
watches the images themselves.

That is the ordinary Helm model, and it holds only as long as each chart keeps up with the
application it packages. Measured on 2026-09-08, half do not: of eighteen charts checked, nine
ship an `appVersion` older than their application's latest release — two of them by a full major
version. The gap is silent. Nothing reports that a chart has stopped tracking its upstream, so
the cluster can sit on a superseded image indefinitely while every dependency signal reads green.

The pins that already existed made the second half of the case. One set an image tag with no
repository beside it, so nothing could resolve which image the version belonged to — and that tag
had been applied by hand to patch an active Critical CVE, which made the pin most in need of
tracking the one nothing could track. Another set an empty-string tag, which is not a pin at all:
it overrides the chart's own default and falls through to `.Chart.AppVersion`, so chart metadata
decided the version while the file appeared to. A mis-specified pin is worse than none, because
it reads as authoritative and is inert.

## Decision

**Every image this cluster actually runs is pinned explicitly in values — registry, repository
and tag together — at the version already running.**

Adopting a pin therefore changes no image. The pin states what is already true, and from that
point the version is decided in this repository rather than inferred from chart metadata.

This applies to all charts, not to a chosen subset. Where a chart deploys several images, all of
them are pinned.

### Limitations

These are properties of charts and registries, not exemptions granted to particular components.

**A chart that exposes no image tag value cannot be pinned.** Some charts render the image
entirely from templates with no override. There is nothing to set, and nothing to decide.

**A tag that cannot be ordered can be pinned but not updated.** Where an image's tags are not
semantic versions, a pin records what runs but no tooling can tell which of two tags is newer.
The pin is still worth having — it makes the version explicit — but it will not produce update
proposals, and that should not be mistaken for the image being current.

**Images belonging to a feature this cluster does not enable are out of scope.** Chart values
frequently carry images for optional subsystems that are never rendered here. Pinning them costs
maintenance, cannot be verified against a running pod, and protects nothing.

### Requirements that come with it

**A chart and the images it packages move in one change.** This is the mechanism that keeps a pin
from falling behind the chart, and it is not optional bookkeeping. In steady state a pin equals
the chart's `appVersion`; every subsequent chart bump therefore moves `appVersion` past the pin
unless the pin moves with it. Grouping the chart and its images into a single update proposal
makes them move together, so the divergence never arises in the normal path.

The same grouping covers images released together within one product. Bumping one alone produces
version skew inside a single application, which is a worse failure than being behind — and it is
a failure the chart maintainer currently prevents on our behalf. Grouping preserves that
guarantee once the versions are ours to set.

This repository already runs exactly this pattern for the Talos version, where two pins in
different files must agree: they share a group so one proposal changes both, and a separate check
fails any change where they disagree. The cost is one grouping rule per component, and the
failure mode when a rule is missing is benign — the chart and image arrive as two proposals, the
divergence check fails the second, and a human is told rather than the drift going unseen.

**Components that cannot be recovered from inside the cluster are never auto-merged.** Where a bad
image would remove the means of fixing it — the network layer, the storage layer, remote
access — updates require human sign-off regardless of how small the version step looks. This uses
the existing `manual-review` label, which the auto-merge gate already treats as a refusal.

Automation is the default everywhere else. Four cases still reach a person, and each is a policy
choice rather than an unhandled edge: the unrecoverable components above; any major version step,
which is already never auto-merged; images whose tags cannot be ordered, where no proposal can be
generated and only the divergence check will say so; and a pin deliberately held away from its
chart, such as one carrying a security patch the chart has not yet shipped.

**Every pin is verified against the running pod, not against the manifest.** Helm silently ignores
values it does not recognise, so a pin at the wrong key path leaves the chart's `appVersion` in
charge while the repository claims otherwise. This failure has occurred here repeatedly and is
invisible by inspection. A pin is not complete until the rendered image and the running image
have been compared and match.

**A pin that falls behind its chart must be detected.** An explicit tag overrides `appVersion`
permanently. While the pin is ahead of the chart it is a patch; the moment the chart's
`appVersion` passes it, the same line becomes a downgrade that nothing announces.

This is not a hypothetical gap in review coverage — the existing auto-merge gate treats it as the
safest possible change. A chart bump that moves `appVersion` past a static pin produces no image
difference in the proposal, and the gate's own branch for that case reads *"chart-only update —
no image CVE scan needed"* and merges it. The condition that most needs a human is the one
currently waved through fastest.

Detection compares the pinned tag against the `appVersion` the deployed release reports. Both
values are already present in the cluster, so the check needs no registry call, no API token and
no chart download. Tags that cannot be ordered are reported as unorderable rather than guessed
at. The check is required, not optional: without it this decision trades one silent staleness for
another.

## Consequences

**The chart/image pairing becomes this repository's responsibility.** Chart maintainers test that
their chart works with the application version they ship. Once versions are pinned here, that
testing no longer covers what runs. Grouping co-released images limits the exposure; it does not
remove it.

**Update volume increases, and that is the point.** A chart that stops keeping up with its
application now surfaces as an image update proposal instead of as silence. Most will be patch
and minor steps.

**Being pinned is not the same as being current.** Two of the components with the largest CVE
exposure lag by a major version, and no automatic image rule should cross a major unattended.
Those remain deliberate upgrades. This decision makes the gap visible and reviewable; it does not
close it.

**Pins age.** A pinned version that nobody advances is a freeze wearing the appearance of a
decision. The detection requirements above are what keep this honest, and they are load-bearing
rather than supplementary.

## Alternatives Considered

**Keep chart-only updating.** Rejected as incomplete: half the charts measured lag their
application and nothing reports it.

**Pin only where a chart deploys a single image.** Rejected. It leaves exactly the largest and
most consequential components unpinned, on the grounds that they are complicated — which inverts
the priority. The complication is real, and grouping is the answer to it rather than exclusion.

**Cap image updates at the current major as the primary mechanism.** Rejected as a substitute,
retained as policy for the pins this creates. Capping closes small gaps and misses large ones,
and the components carrying real exposure lag by majors.

**Alert on image age instead of pinning.** Rejected as a replacement, kept as a complement.
Age measured against our own pin misfires wherever an override already exists. Upstream project
liveness is the signal worth alerting on, and is tracked separately.
