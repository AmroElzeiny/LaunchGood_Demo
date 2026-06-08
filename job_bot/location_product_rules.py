from __future__ import annotations

REMOTE_GLOBAL_RULE = (
    "Remote Global means only this: the opportunity is remote and not limited by country, "
    "nationality, citizenship, residence, or work authorization tied to a country."
)

REMOTE_WITHIN_COUNTRY_RULE = (
    "Remote within a country means only this: the opportunity is remote and explicitly limited "
    "to one country or country-specific location scope."
)

REMOTE_GLOBAL_UI_HELP = (
    "Pick Remote Global only if you want remote projects, contracts, or consulting briefs with no country, nationality, "
    "citizenship, residence, or country-tied work-authorization restriction."
)

REMOTE_WITHIN_COUNTRY_UI_HELP = (
    "Pick Remote within a country only if you want remote projects or contracts explicitly limited "
    "to one country or country-specific location scope."
)

REMOTE_SCOPE_PROMPT_BLOCK = (
    f"{REMOTE_GLOBAL_RULE} "
    "Accept Remote Global when the opportunity is remote and there is no country, nationality, "
    "citizenship, residence, or country-tied work-authorization restriction. "
    "Do not reject Remote Global because of timezone preference, payroll wording, "
    "team distribution wording, or optional office hubs.\n"
    f"{REMOTE_WITHIN_COUNTRY_RULE} "
    "Accept Remote within a country only when the opportunity is remote and explicitly limited "
    "to that country or a location scope inside that country. Reject globally remote roles "
    "for this mode.\n"
    "Remote Global positive examples: 'work from anywhere', 'remote worldwide', "
    "'distributed team, work from any country'.\n"
    "Remote Global negative examples: 'Egypt only', 'must be based in Germany', "
    "'EU citizenship required', 'right to work in Canada required'.\n"
    "Remote within a country positive examples: 'remote within Egypt', "
    "'must be based in Poland', 'remote role for candidates in India'.\n"
    "Remote within a country negative examples: 'work from anywhere', "
    "'remote worldwide', 'global remote with optional London hub'."
)
