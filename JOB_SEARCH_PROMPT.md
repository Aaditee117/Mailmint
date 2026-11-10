# Master prompt — find jobs, internships, and freelance opportunities

Paste everything between the `=== PROMPT START ===` and `=== PROMPT END ===` markers
into a new Claude Code session (with the Apify connector available) to run a job
search from scratch, without needing any prior chat context.

---

=== PROMPT START ===

I want you to find me relevant jobs, internships, and freelance opportunities. Use
the Apify connector to search multiple platforms directly — do not rely on generic
web search or stop after 3-5 results.

## MY PROFILE

- Aaditee Agrawal, B.E. Computer Science Engineering, Chandigarh University, Mohali,
  Punjab, India (graduating 2026)
- Currently: Software Engineer Intern at Optimus Information Inc. (since Mar 2026),
  stipend ~₹20,000/month
- Core skills: Python, SQL, C++, Microsoft Fabric, PySpark, Delta Lake, Azure Data
  Factory, Azure (Blob Storage, ADLS Gen2, Cosmos DB), Power BI (DAX, dashboards),
  REST APIs, Docker, Git/GitHub
- Two resume variants in the project root: `Software-resume.pdf` emphasizes Software
  Engineering (OOP, DSA, cloud-native apps, AI-assisted dev); `Data-resume.pdf`
  emphasizes Data Engineering (ETL/ELT pipelines, data quality frameworks, data
  warehousing, medallion lakehouse architecture)
- Certifications: Microsoft Fabric Data Engineer Associate (DP-700), Fabric Analytics
  Engineer Associate (DP-600), Azure Fundamentals (AZ-900)
- Read both resume PDFs from the project root before searching

## WHAT TO SEARCH FOR

- Roles: Data Engineer, Software Engineer, Data Analyst — entry-level/fresher (0-2
  yrs) preferred, but also surface stretch roles requiring slightly more experience
  if the skill match is strong
- Location: India (any city) or Remote
- Recency: prioritize postings from the last 7 days; widen to 30 days if a search
  comes back thin
- Include: full-time jobs, internships, AND freelance/contract gigs — don't stop at
  just one category

## PAY THRESHOLD

- Internships: minimum stipend ₹20,000/month
- EXCEPTION: if an internship is fully remote, a lower or undisclosed stipend is
  still acceptable — surface it anyway
- Full-time roles and freelance gigs: no strict minimum, but note the pay/rate when
  disclosed

## SOURCES — search ALL of these, not just one or two

- LinkedIn Jobs
- Naukri.com
- Internshala (India's largest internship board — don't skip this one)
- Indeed
- Upwork (for freelance/contract work)

## LEGITIMACY CHECK

When a company's stipend/salary is undisclosed, cross-reference AmbitionBox and/or
Glassdoor for that company's typical pay for similar roles/experience levels — use
this as a proxy for whether the internship pay is credible and whether the company
is a real, verifiable employer (vs. an obscure listing with zero public presence,
which is a caution flag).

## OUTPUT FORMAT

- Group results into clear categories (e.g. "Strong fit — clears pay bar", "Remote —
  relaxed pay bar", "Notable brands — undisclosed pay", "Freelance gigs")
- For each listing: role, company, location, stipend/salary if known, and a DIRECT
  APPLICATION LINK
- Flag anything suspicious (extremely low "token" stipends like ₹300/month, no
  company footprint anywhere, vague generic titles)
- Don't silently trim results — if you cut anything for length, say what and how
  many, and offer to expand

If a search comes back with very few results, treat that as a signal to broaden —
try more platforms, more keyword variants, or a longer time window — rather than
presenting a thin result set as the full picture.

=== PROMPT END ===
