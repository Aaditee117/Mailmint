"""Turns the recipient list into a concrete send plan.

Shared by preview and send so that what you review is exactly what goes out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .dnscheck import MxResult, discover_domain, lookup_mx
from .patterns import Candidate, infer_pattern_from_sample, rank_candidates
from .people import Person, group_by_domain_scope
from .store import LearnedStore, SentLog


@dataclass
class DomainPlan:
    scope: str
    domain: str
    mx: MxResult | None
    pattern: str | None
    pattern_source: str
    plausible: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return self.mx.provider if self.mx else "unknown"


@dataclass
class PersonPlan:
    person: Person
    domain_plan: DomainPlan
    candidates: list[Candidate]
    chosen: Candidate | None
    # What to try next if `chosen` bounces: ranked, and already filtered of
    # addresses this person has been mailed at before.
    alternatives: list[Candidate] = field(default_factory=list)
    skip_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def sendable(self) -> bool:
        return self.chosen is not None and not self.skip_reason


def resolve_domain(
    scope: str,
    people: list[Person],
    config: Config,
    learned: LearnedStore,
    allow_discovery: bool,
) -> DomainPlan:
    """Settle on a mail domain for one company and work out its address pattern."""
    knowledge = learned.get(scope)
    notes: list[str] = []

    domain = next((p.domain for p in people if p.domain), "") or knowledge.domain
    company = next((p.company for p in people if p.company), "")

    if not domain and allow_discovery and company:
        winner, attempts = discover_domain(company, config.domain_tlds)
        plural = "" if len(attempts) == 1 else "s"
        if winner:
            domain = winner.domain
            notes.append(
                f"domain GUESSED from the company name after {len(attempts)} "
                f"candidate{plural} - a domain that accepts mail is not proof it "
                f"belongs to '{company}'. Open it in a browser before sending."
            )
        else:
            notes.append(
                f"could not find a mail domain for '{company}' "
                f"({len(attempts)} candidate{plural} tried)"
            )

    mx = lookup_mx(domain) if domain else None
    if mx and not mx.ok:
        prefix = "" if mx.conclusive else "could not verify - "
        notes.append(f"{domain}: {prefix}{mx.error}")

    # A confirmed sample beats everything else we could compute.
    pattern = knowledge.pattern
    source = knowledge.pattern_source
    plausible: list[str] = []

    for person in people:
        if not person.sample_email:
            continue
        confirmed, candidates, explanation = infer_pattern_from_sample(
            person.sample_email, person.sample_name
        )
        if confirmed:
            pattern, source = confirmed, explanation
            plausible = candidates
            break
        if candidates and not plausible:
            plausible = candidates
            notes.append(explanation)

    ruled_out = set(knowledge.ruled_out)
    if pattern and pattern in ruled_out:
        notes.append(f"pattern '{pattern}' previously bounced for this domain - ignoring it")
        pattern, source = None, ""

    plan = DomainPlan(
        scope=scope,
        domain=domain,
        mx=mx,
        pattern=pattern,
        pattern_source=source,
        plausible=[p for p in plausible if p not in ruled_out],
        notes=notes,
    )

    knowledge.domain = domain
    knowledge.provider = plan.provider
    knowledge.mx_ok = bool(mx and mx.ok)
    if pattern:
        knowledge.pattern = pattern
        knowledge.pattern_source = source
    return plan


def build_plans(
    people: list[Person],
    config: Config,
    learned: LearnedStore,
    sent_log: SentLog,
    allow_discovery: bool = True,
    retry_bounced: bool = False,
) -> tuple[list[PersonPlan], dict[str, DomainPlan]]:
    domain_plans: dict[str, DomainPlan] = {}
    person_plans: list[PersonPlan] = []
    seen_keys: set[str] = set()

    for scope, group in group_by_domain_scope(people).items():
        domain_plans[scope] = resolve_domain(scope, group, config, learned, allow_discovery)

    for person in people:
        scope = (person.domain or person.company).strip().lower()
        domain_plan = domain_plans[scope]
        warnings = list(person.problems())
        skip = ""
        candidates: list[Candidate] = []
        chosen: Candidate | None = None

        already = sent_log.already_contacted(person.key)
        if already:
            skip = f"already emailed on {already.timestamp[:10]} at {already.email}"
        elif person.key in seen_keys:
            skip = "duplicate row"
        elif not person.name.first:
            skip = "no usable first name"
        elif not domain_plan.domain:
            skip = "no mail domain"
        elif domain_plan.mx and domain_plan.mx.rejects_mail:
            skip = f"domain does not accept mail ({domain_plan.mx.error})"
        elif domain_plan.mx and not domain_plan.mx.ok:
            warnings.append(f"MX unverified ({domain_plan.mx.error}) - sending anyway")

        seen_keys.add(person.key)

        if person.known_email:
            candidates = [Candidate("explicit", person.known_email, 1.0)]
        elif not skip:
            sector = person.sector or config.default_sector
            ruled_out = set(learned.get(scope).ruled_out)
            ranked = rank_candidates(
                person.name,
                domain_plan.domain,
                sector=sector,
                provider=domain_plan.provider,
                learned_pattern=domain_plan.pattern,
            )
            # Nudge structurally-plausible shapes up when we have a hint but no
            # confirmed pattern for the domain.
            if not domain_plan.pattern and domain_plan.plausible:
                boost = set(domain_plan.plausible)
                ranked = sorted(
                    ranked,
                    key=lambda c: (c.pattern in boost, c.confidence),
                    reverse=True,
                )
            candidates = [c for c in ranked if c.pattern not in ruled_out]

        tried = sent_log.addresses_tried(person.key)
        fresh = [c for c in candidates if c.email.lower() not in tried]
        if tried and not retry_bounced:
            fresh = []
            if not skip:
                skip = f"already tried {len(tried)} address(es); use --retry-bounced to try the next guess"
        if fresh:
            chosen = fresh[0]
        elif not skip:
            skip = "no candidate addresses could be built"

        person_plans.append(
            PersonPlan(
                person=person,
                domain_plan=domain_plan,
                candidates=candidates,
                chosen=chosen,
                alternatives=fresh[1:],
                skip_reason=skip,
                warnings=warnings,
            )
        )

    return person_plans, domain_plans
