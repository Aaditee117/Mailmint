"""MX lookups and domain discovery.

DNS is the only free check that tells the truth. It confirms a domain accepts
mail and identifies who runs it - it says nothing about whether a specific
mailbox exists, and nothing here pretends otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import dns.exception
import dns.resolver

# Ordered: the first substring to match a bare MX hostname wins, so put the
# specific gateways ahead of the generic vendor domains.
_PROVIDER_SIGNATURES: list[tuple[str, str]] = [
    ("pphosted.com", "gateway"),
    ("proofpoint", "gateway"),
    ("mimecast", "gateway"),
    ("messagelabs", "gateway"),
    ("barracudanetworks", "gateway"),
    ("trendmicro", "gateway"),
    ("sophos", "gateway"),
    ("protection.outlook.com", "microsoft"),
    ("mail.protection.outlook", "microsoft"),
    ("outlook.com", "microsoft"),
    ("aspmx.l.google.com", "google"),
    ("googlemail.com", "google"),
    ("google.com", "google"),
    ("zoho", "zoho"),
    ("secureserver.net", "godaddy"),
    ("improvmx", "forwarder"),
    ("forwardemail", "forwarder"),
    ("mailgun", "forwarder"),
    ("sendgrid", "forwarder"),
]

PROVIDER_LABELS = {
    "google": "Google Workspace",
    "microsoft": "Microsoft 365",
    "zoho": "Zoho Mail",
    "gateway": "Enterprise gateway",
    "godaddy": "GoDaddy",
    "forwarder": "Forwarding service",
    "other": "Self-hosted / other",
    "unknown": "Unknown",
}

# Words that appear in a company's legal name but never in its domain.
_COMPANY_NOISE = {
    "pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp",
    "corp", "corporation", "co", "company", "technologies", "technology",
    "tech", "solutions", "systems", "services", "software", "labs", "group",
    "global", "india", "consulting", "and", "the",
}


@dataclass
class MxResult:
    domain: str
    ok: bool
    exchanges: list[str] = field(default_factory=list)
    provider: str = "unknown"
    error: str = ""
    # False when DNS itself failed (timeout, no resolver) rather than the domain
    # giving a real answer. An inconclusive result must never be read as "this
    # domain rejects mail" - that would silently skip everyone on a bad network.
    conclusive: bool = True

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    @property
    def rejects_mail(self) -> bool:
        """True only when DNS positively told us this domain takes no mail."""
        return self.conclusive and not self.ok


def classify_provider(exchanges: list[str]) -> str:
    joined = " ".join(exchanges).lower()
    for signature, provider in _PROVIDER_SIGNATURES:
        if signature in joined:
            return provider
    return "other" if exchanges else "unknown"


# Public resolvers to fall back to. Corporate and VPN nameservers frequently
# refuse or blackhole external MX queries, and dnspython will spend its entire
# timeout budget on the first dead server rather than moving on - so we pick a
# nameserver that demonstrably works, once, and reuse it.
_FALLBACK_SERVERS: list[str] = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]
_PER_SERVER_TIMEOUT = 2.5
_PROBE_DOMAIN = "gmail.com"


def configure_resolver(fallback_servers: list[str] | None, timeout: float | None) -> None:
    """Override the fallback resolvers and per-server timeout from config."""
    global _FALLBACK_SERVERS, _PER_SERVER_TIMEOUT
    if fallback_servers:
        _FALLBACK_SERVERS = list(fallback_servers)
    if timeout:
        _PER_SERVER_TIMEOUT = float(timeout)
    active_resolver.cache_clear()
    lookup_mx.cache_clear()


@lru_cache(maxsize=1)
def active_resolver() -> tuple[dns.resolver.Resolver | None, str]:
    """Find a nameserver that actually answers, and remember it.

    Returns (resolver, description). A None resolver means nothing answered,
    which is a network problem rather than a fact about any domain.
    """
    try:
        system = list(dns.resolver.Resolver().nameservers)
    except dns.exception.DNSException:
        system = []

    seen: set[str] = set()
    candidates = [
        str(ns) for ns in system + _FALLBACK_SERVERS
        if not (str(ns) in seen or seen.add(str(ns)))
    ]

    skipped: list[str] = []
    for nameserver in candidates:
        probe = dns.resolver.Resolver(configure=False)
        probe.nameservers = [nameserver]
        probe.timeout = _PER_SERVER_TIMEOUT
        probe.lifetime = _PER_SERVER_TIMEOUT
        try:
            probe.resolve(_PROBE_DOMAIN, "MX")
        except dns.exception.DNSException:
            skipped.append(nameserver)
            continue
        note = f"using nameserver {nameserver}"
        if skipped:
            note += f" (skipped {len(skipped)} that did not answer: {', '.join(skipped)})"
        return probe, note

    return None, (
        "no nameserver answered - tried " + ", ".join(candidates) +
        ". MX checks will be skipped; addresses can still be guessed and sent."
    )


@lru_cache(maxsize=512)
def lookup_mx(domain: str) -> MxResult:
    """Resolve a domain's MX records. Cached: `discover` revisits domains a lot."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or "." not in domain:
        return MxResult(domain, False, error="not a domain")

    resolver, _ = active_resolver()
    if resolver is None:
        return MxResult(
            domain, False, error="no working nameserver (DNS check skipped)",
            conclusive=False,
        )

    try:
        answers = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        return MxResult(domain, False, error="domain does not exist")
    except dns.resolver.NoAnswer:
        return MxResult(domain, False, error="no MX record (domain does not receive mail)")
    except dns.resolver.NoNameservers:
        return MxResult(domain, False, error="no nameservers responded", conclusive=False)
    except dns.exception.Timeout:
        return MxResult(domain, False, error="DNS timeout", conclusive=False)
    except dns.exception.DNSException as exc:
        return MxResult(
            domain, False, error=f"DNS error: {exc.__class__.__name__}", conclusive=False
        )

    hosts = [str(record.exchange).rstrip(".").lower() for record in answers]
    if not hosts:
        return MxResult(domain, False, error="empty MX record set")
    # RFC 7505: a lone "." exchange is a null MX, an explicit declaration that
    # the domain receives no mail. It arrives looking like a successful lookup,
    # so it has to be caught here or it reads as the opposite of what it means.
    if all(not host for host in hosts):
        return MxResult(domain, False, error="null MX - domain accepts no mail (RFC 7505)")

    exchanges = sorted(host for host in hosts if host)
    return MxResult(domain, True, exchanges, classify_provider(exchanges))


def company_slugs(company: str) -> list[str]:
    """Turn a company name into the domain stems worth trying, best first."""
    ascii_name = re.sub(r"[^A-Za-z0-9 ]", " ", company or "")
    tokens = [t.lower() for t in ascii_name.split() if t]
    meaningful = [t for t in tokens if t not in _COMPANY_NOISE] or tokens
    if not meaningful:
        return []

    slugs: list[str] = ["".join(meaningful)]
    if len(meaningful) > 1:
        slugs.append(meaningful[0])
        slugs.append("".join(t[0] for t in meaningful))
        slugs.append("-".join(meaningful))
    # The full token list including noise words, in case "techsolutions" really
    # is part of the domain.
    if tokens != meaningful:
        slugs.append("".join(tokens))

    seen: set[str] = set()
    return [s for s in slugs if len(s) > 1 and not (s in seen or seen.add(s))]


def discover_domain(company: str, tlds: list[str]) -> tuple[MxResult | None, list[MxResult]]:
    """Try candidate domains for a company name, return the first that takes mail.

    Returns (winner, all_attempts). A winner here means "this domain receives
    mail", not "this is definitely the right company" - always eyeball it.
    """
    attempts: list[MxResult] = []
    for slug in company_slugs(company):
        for tld in tlds:
            result = lookup_mx(f"{slug}{tld}")
            attempts.append(result)
            if result.ok:
                return result, attempts
    return None, attempts
