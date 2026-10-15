# Concurrency tests: many threads hit the SAME accounts at the same instant, each with its own
# database connection, exactly like simultaneous API requests. They prove the row locks work:
# no lost updates, no negative balances, no deadlocks, and the total money never changes.

import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import database
import ledger

HEALTHY = {"total_balance_paise": 0, "accounts_mismatched": 0, "transfers_unbalanced": 0}


def run_parallel(fn, arg_list):
    """Run fn(*args) for every args in arg_list, each in its own thread, all starting together.
    Returns the results (or the exception each call raised) in order."""
    barrier = threading.Barrier(len(arg_list))  # nobody starts until every thread is ready

    def call(args):
        barrier.wait()
        try:
            return fn(*args)
        except Exception as e:  # collect errors instead of losing them inside the thread
            return e

    with ThreadPoolExecutor(max_workers=len(arg_list)) as pool:
        return list(pool.map(call, arg_list))


def transfer_in_own_session(src, dst, paise):
    """One 'request': its own session (= its own DB connection and transaction)."""
    with database.SessionLocal() as s:
        ledger.transfer(s, src, dst, paise)
        s.commit()
        return "ok"


def balances(session, *ids):
    session.expire_all()  # forget cached values; read what's in the database now
    return [ledger.get_account(session, i).balance_paise for i in ids]


def test_100_parallel_transfers_between_the_same_two_accounts(session):
    a = ledger.create_account(session, "A", 100_000).id
    b = ledger.create_account(session, "B", 0).id
    results = run_parallel(transfer_in_own_session, [(a, b, 100)] * 100)
    assert results == ["ok"] * 100
    # Without locking, some updates are lost (two threads read the same old balance).
    assert balances(session, a, b) == [100_000 - 100 * 100, 100 * 100]
    assert ledger.check_invariants(session) == HEALTHY


def test_opposite_directions_do_not_deadlock(session):
    # A->B locks A then B; B->A would lock B then A -> a classic deadlock. Locking in ascending
    # id order makes both lock A first, so one simply waits for the other.
    a = ledger.create_account(session, "A", 100_000).id
    b = ledger.create_account(session, "B", 100_000).id
    results = run_parallel(transfer_in_own_session, [(a, b, 100)] * 50 + [(b, a, 100)] * 50)
    assert results == ["ok"] * 100  # a deadlock would show up here as an exception
    assert balances(session, a, b) == [100_000, 100_000]
    assert ledger.check_invariants(session) == HEALTHY


def test_overdraft_race_lets_exactly_the_affordable_transfers_through(session):
    # A can afford exactly 50 transfers of 100. 100 race for it: 50 must win, 50 must be refused.
    a = ledger.create_account(session, "A", 5_000).id
    b = ledger.create_account(session, "B", 0).id
    results = run_parallel(transfer_in_own_session, [(a, b, 100)] * 100)
    assert results.count("ok") == 50
    assert sum(isinstance(r, ledger.InsufficientFunds) for r in results) == 50
    assert balances(session, a, b) == [0, 5_000]
    assert ledger.check_invariants(session) == HEALTHY


def test_same_idempotency_key_sent_20_times_at_once_moves_money_once(client, session):
    a = ledger.create_account(session, "A", 10_000).id
    b = ledger.create_account(session, "B", 0).id
    body = {"from_account_id": a, "to_account_id": b, "amount_paise": 1_000}

    def post():
        return client.post("/transfers", json=body, headers={"Idempotency-Key": "same-key"})

    responses = run_parallel(post, [()] * 20)
    assert [r.status_code for r in responses] == [201] * 20
    assert len({r.content for r in responses}) == 1  # all 20 got the very same response
    assert sum("Idempotent-Replayed" not in r.headers for r in responses) == 1  # 1 did the work
    assert balances(session, a, b) == [9_000, 1_000]


def test_total_money_never_changes_under_concurrent_random_transfers(session):
    rng = random.Random(11)
    ids = [ledger.create_account(session, f"u{i}", 20_000).id for i in range(10)]
    total_before = sum(balances(session, *ids))
    jobs = [(*rng.sample(ids, 2), rng.randint(1, 8_000)) for _ in range(300)]

    results = run_parallel(transfer_in_own_session, jobs)
    # Every call either succeeded or was refused for lack of funds: no deadlocks, no other errors.
    assert all(r == "ok" or isinstance(r, ledger.InsufficientFunds) for r in results)
    assert sum(balances(session, *ids)) == total_before
    assert min(balances(session, *ids)) >= 0
    assert ledger.check_invariants(session) == HEALTHY


def test_parallel_account_openings_are_all_funded_correctly(client, session):
    # Every opening balance debits the shared system account: it must not lose updates either.
    def open_one(i):
        return client.post("/accounts", json={"owner_name": f"p{i}",
                                              "initial_balance_paise": 1_000}).status_code

    assert run_parallel(open_one, [(i,) for i in range(50)]) == [201] * 50
    assert balances(session, ledger.system_account_id(session)) == [-50 * 1_000]
    assert ledger.check_invariants(session) == HEALTHY


def test_unique_keys_in_parallel_are_all_distinct_transfers(client, session):
    a = ledger.create_account(session, "A", 10_000).id
    b = ledger.create_account(session, "B", 0).id
    body = {"from_account_id": a, "to_account_id": b, "amount_paise": 100}

    def post():
        return client.post("/transfers", json=body,
                           headers={"Idempotency-Key": str(uuid.uuid4())}).status_code

    assert run_parallel(post, [()] * 30) == [201] * 30
    assert balances(session, a, b) == [7_000, 3_000]
