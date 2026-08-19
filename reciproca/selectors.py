"""
Reciproca - in-browser scripts and login selectors.

The JavaScript constants are kept here rather than inlined so the row-walking
logic can be exercised against a synthetic DOM in tests (tests/test_extraction.js
parses this file) - that walk is the fragile part of the feature. All scripts are
verbatim copies of the monolith's.
"""

# ---------------------------
# FOLLOWERS-DIALOG EXTRACTION SCRIPT
# ---------------------------
# Runs inside the browser against the open followers dialog. Kept as a named
# constant rather than inlined so the row-walking logic can be exercised against
# a synthetic DOM in tests - that walk is the fragile part of this feature.
#
# Called with one argument: the list of "already following" markers, passed in
# from FOLLOWING_BUTTON_MARKERS so locale strings stay defined in exactly one
# place. Returns {kept, skippedFollowing, rowsWithoutButton, rowsInspected}.
EXTRACT_FOLLOWERS_JS = r"""
const followingMarkers = arguments[0];
const RESERVED = ['p', 'explore', 'accounts', 'direct', 'emails', 'reels', 'stories',
                  'help', 'about', 'blog', 'jobs', 'privacy', 'terms', 'locations',
                  'language', 'developers', 'settings'];

const dialog = document.querySelector("div[role='dialog']");
if (!dialog) return {kept: [], skippedFollowing: 0, rowsWithoutButton: 0, rowsInspected: 0};

// Username out of an href like "/mario/" - null for anything that is not a
// plain profile link.
function usernameFromLink(link) {
    const href = link.getAttribute('href');
    if (!href) return null;
    const match = href.match(/^\/([^/]+)\/?$/);
    if (!match) return null;
    const username = match[1];
    if (!username || username.length <= 1) return null;
    if (RESERVED.includes(username)) return null;
    if (username.includes('.') || username.includes('?')) return null;
    if (username.startsWith('__') || username.startsWith('dm_')) return null;
    return username;
}

function usersInside(node) {
    const found = new Set();
    node.querySelectorAll('a[href^="/"]').forEach(a => {
        const u = usernameFromLink(a);
        if (u) found.add(u);
    });
    return found;
}

// Smallest ancestor that still belongs to this user alone and carries a button.
// Two stop conditions, whichever comes first: the node already holds a button,
// or the next step up would swallow a different user's link (a row boundary).
// The depth cap is purely defensive, so an unexpected DOM cannot walk to <body>.
function findRow(link, username) {
    let row = link;
    for (let depth = 0; depth < 8; depth++) {
        if (row.querySelector('button')) return row;
        const parent = row.parentElement;
        if (!parent || parent === dialog) break;
        const users = usersInside(parent);
        let foreign = false;
        users.forEach(u => { if (u !== username) foreign = true; });
        if (foreign) break;
        row = parent;
    }
    return row.querySelector('button') ? row : null;
}

const kept = [];
const seen = new Set();
let skippedFollowing = 0;
let rowsWithoutButton = 0;
let rowsInspected = 0;

dialog.querySelectorAll('a[href^="/"]').forEach(link => {
    const username = usernameFromLink(link);
    if (!username || seen.has(username)) return;
    seen.add(username);
    rowsInspected++;

    const row = findRow(link, username);
    if (!row) {
        // Fail open: keeping a candidate is far less harmful than silently
        // dropping everyone if Instagram's markup changes. rowsWithoutButton
        // is what makes such a regression visible instead of silent.
        rowsWithoutButton++;
        kept.push(username);
        return;
    }

    let following = false;
    row.querySelectorAll('button').forEach(btn => {
        const text = (btn.innerText || btn.textContent || '').toLowerCase();
        if (followingMarkers.some(m => text.includes(m))) following = true;
    });

    if (following) {
        skippedFollowing++;
    } else {
        kept.push(username);
    }
});

return {kept, skippedFollowing, rowsWithoutButton, rowsInspected};
"""

# Every post link currently on a hashtag page, as the href attribute reads in the
# DOM. Collected in one call rather than one round trip per element: a scrolled
# hashtag page holds hundreds of them, and the values are also how each post is
# found again right before it is clicked, which keeps element references from
# going stale while post dialogs open and close.
POST_LINKS_JS = """
// An open post is not the grid, and its links belong to that post rather than to the
// hashtag, so there is nothing here worth collecting. null asks the caller to close it.
if (document.querySelector("div[role='dialog']")) return null;

// A grid tile links straight to the post and no deeper. An open post also carries a
// link per comment (/p/<code>/c/<id>/), one for its likes (/p/<code>/liked_by/) and
// its own permalink (/<user>/p/<code>/): all contain "/p/" without being tiles.
// A query string is still that same post, and dropping tiles over one would look
// like a hashtag with no posts, which is the worse way to be wrong here.
const TILE = /^\\/p\\/[^/?]+\\/?(\\?.*)?$/;

const hrefs = [];
const seen = new Set();
document.querySelectorAll("a[href*='/p/']").forEach(a => {
    const href = a.getAttribute('href');
    if (href && TILE.test(href) && !seen.has(href)) {
        seen.add(href);
        hrefs.push(href);
    }
});
return hrefs;
"""

# The three numbers in a profile header, read where the browser already is when a
# follow is about to happen. Returns raw strings rather than numbers, so the
# parsing - and every locale quirk in it - stays in Python where it is tested.
#
# The follower and following counts are found by their links, which are structural
# and survive Instagram's redesigns better than any class name. Where the visible
# text is abbreviated ("12.3K") the exact figure is usually in a title attribute
# alongside it, so that is preferred. The post count has no link, so it is left to
# a search of the header text.
#
# Every matching link is returned, not the first. More than one of them points at a
# profile's followers: beside the count there is the line about people you both
# follow, which reads "11 followers you follow" and leads to the same page. Which
# comes first in the markup is Instagram's business, so choosing between them is
# left to Python, where it is tested.
PROFILE_STATS_JS = """
const header = document.querySelector('header');
if (!header) return null;

function read(link) {
    // The title may be on the link itself, which querySelector would not reach.
    const titled = link.matches('[title]') ? link : link.querySelector('[title]');
    const title = titled ? titled.getAttribute('title') : null;
    // The address takes no part in choosing the count, which goes by what the link
    // says. It is carried so check_profile.py can show which link a number came
    // from, the one thing a log line cannot tell you when a count looks wrong.
    return {
        href: link.getAttribute('href') || '',
        title: title,
        text: link.innerText || link.textContent || '',
    };
}

function readAll(selector) {
    return Array.from(header.querySelectorAll(selector)).map(read);
}

// Matched loosely on purpose: the href is not always exactly "/user/followers/" -
// it can carry a query string or lose its trailing slash.
return {
    headerText: header.innerText || header.textContent || '',
    followers: readAll('a[href*="/followers"]'),
    following: readAll('a[href*="/following"]'),
};
"""

# The login form's username input. Instagram has moved the field from
# name="username" to name="email" (same box for username, email or phone); the
# autocomplete attribute is the marker that has stayed put. Try them in order,
# ending with the label's for->id link, which survives attribute renames.
LOGIN_USERNAME_SELECTORS = (
    "input[name='username']",
    "input[autocomplete^='username']",
    "input[name='email']",
)
LOGIN_USERNAME_XPATHS = (
    # The login label ("Numero di cellulare, nome utente o indirizzo e-mail" or
    # the English equivalent) points at the input through its for attribute.
    "//input[@id=//label[contains(text(), 'utente') or contains(text(), 'username')"
    " or contains(text(), 'cellulare') or contains(text(), 'phone')]/@for]",
)
