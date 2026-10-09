"""Check the Supabase connection and the vacation schema with uv."""
from __future__ import annotations

from planner.sql_repository import SqlAlchemyRepository


def main() -> None:
    repository = SqlAlchemyRepository.from_env()
    details = repository.ping()
    state = repository.snapshot()
    print(f"Connected to {details['database']} as {details['user']}.")
    print(f"Teams: {len(state['teams'])}; employees: {len(state['employees'])}; ranges: {len(state['ranges'])}; holidays: {len(state['holidays'])}.")
    print("Supabase vacation schema is readable.")


if __name__ == "__main__":
    main()

