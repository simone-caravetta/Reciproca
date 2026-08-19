"""Show every number Reciproca reads off one profile, and where each came from.

Run it when a count in the log looks wrong. It opens the app's own Chrome profile,
so it sees the page as your logged-in account does, and it prints what each route to
each count found rather than only the answer - which is what tells a misread apart
from a profile that really is what it looks like.

    python check_profile.py fanfanz95

Close Reciproca first: Chrome will not open the same profile directory twice.
"""
import sys
import time

import reciproca as R


def report(username):
    R.state.driver.get(f"https://www.instagram.com/{username}/")
    time.sleep(4)

    raw = R.state.driver.execute_script(R.PROFILE_STATS_JS)
    if not raw:
        print("No header on the page: either not logged in, or it did not load.")
        return

    header_text = raw.get("headerText") or ""
    print("\nThe header, as the browser renders it:")
    print("  " + header_text.replace("\n", " / "))

    for name, markers in (
        ("followers", R.FOLLOWERS_LABEL_MARKERS),
        ("following", R.FOLLOWING_LABEL_MARKERS),
    ):
        print(f"\nLinks to /{name} in that header:")
        for entry in raw.get(name) or []:
            value = R.count_link_value(entry, markers)
            text = (entry.get("text") or "").replace("\n", " ")
            verdict = f"the count, {value}" if value is not None else "not the count"
            print(f"  href={entry.get('href')!r}")
            print(f"    {text!r} title={entry.get('title')!r} -> {verdict}")
        if not raw.get(name):
            print("  none")
        print(f"  the header text says: {R.parse_labelled_count(header_text, markers)}")

    described = R.profile_description(header_text)
    print("\nWhat this profile says about itself, as the ranking reads it:")
    if described is None:
        print("  nothing readable, so this profile would go unscored")
    else:
        for line in described.splitlines():
            print(f"  {line}")

    posts, followers, following = R.read_profile_stats()
    print(
        f"\nWhat the bot filter is given: "
        f"posts={posts} followers={followers} following={following}"
    )
    print(f"Verdict: {R.bot_rejection_reason(posts, followers, following) or 'follow it'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: python {sys.argv[0]} <username>")

    # log() goes to the log file and the terminal now; no GUI is running here.
    R.open_browser()
    if R.state.driver is None:
        sys.exit("Chrome did not open - see the log above.")
    try:
        report(sys.argv[1].strip().strip("/").split("/")[-1])
    finally:
        R.handle_browser_closed()
