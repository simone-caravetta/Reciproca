"""
Reciproca - Instagram UI text markers.

Instagram renders its interface in the account's own language, so every button
and state lookup has to match text in each supported locale.

Supported locales: English, Italian.

These are the single source of truth - do not inline locale strings at the call
sites. Adding a language should mean editing this block and nothing else.
All comparisons run against lowercased text, so keep every entry lowercase.
"""

# Text on the button meaning "you already follow this account" (or a follow request
# is pending). This is also the button you click to start an unfollow.
FOLLOWING_BUTTON_MARKERS = (
    "following", "requested",                            # EN
    "segui già", "seguendo", "richiesta", "in attesa",    # IT
)

# Text on the plain "Follow" button. Note these are substrings of the markers above
# ("follow" of "following", "segui" of "segui già"), so never test them on their
# own - use is_follow_button(), which excludes the already-following case.
FOLLOW_BUTTON_MARKERS = (
    "follow",   # EN
    "segui",    # IT
)

# Text that merely *signals* an existing relationship without being the follow
# button itself: the Message button only appears on profiles you already follow.
# Safe for lenient post-click validation, never for deciding what to click.
FOLLOWED_SIGNAL_MARKERS = FOLLOWING_BUTTON_MARKERS + (
    "message",     # EN
    "messaggio",   # IT
)

# Text on the confirmation button in the "Unfollow?" dialog.
UNFOLLOW_CONFIRM_MARKERS = (
    "unfollow",                 # EN
    "non seguire", "smetti",    # IT
)

# The words beside the three counts in a profile header. The follower and following
# counts are normally found by their links, but those links are not guaranteed to be
# there, so reading the header text is the fallback for all three.
#
# Longest first: Italian uses "follower" for any number, so the English plural has to
# be tried before the form that is also a prefix of it.
POSTS_LABEL_MARKERS = (
    "post",                     # EN "posts" / IT "post"
)
FOLLOWERS_LABEL_MARKERS = (
    "followers", "follower",    # EN / IT
)
FOLLOWING_LABEL_MARKERS = (
    "following", "seguiti",     # EN / IT
)

# Labels on a post dialog's close button. Matched via XPath contains(), which is
# case-sensitive, so these keep their original capitalization.
CLOSE_BUTTON_LABELS = ("Close", "Chiudi")

# The line naming the people who follow an account and who you follow too. It sits
# in the header right after the bio, so it is one of the two things that mark where
# the bio ends.
MUTUAL_FOLLOWERS_MARKERS = (
    "followed by",          # EN
    "account seguito da",   # IT
)

# The buttons under a profile's bio. Whole lines are matched against these rather
# than searched for inside them: "segui" appears in plenty of real bios, and a bio
# reading "seguimi su youtube" must not be cut off at its first word.
PROFILE_BUTTON_LABELS = FOLLOW_BUTTON_MARKERS + FOLLOWED_SIGNAL_MARKERS

# Page or dialog text Instagram shows when it is throttling or blocking actions,
# paired with the explanation surfaced in the log.
RATE_LIMIT_MARKERS = (
    # EN
    ("try again later", "Try Again Later - Instagram needs you to slow down"),
    ("action blocked", "Action Blocked - You've exceeded a limit"),
    ("temporarily blocked", "Temporarily Blocked - Instagram locked your actions"),
    ("too many requests", "Too Many Requests - You're hitting the API too fast"),
    ("please wait", "Please Wait - Instagram is throttling you"),
    # IT
    ("riprova più tardi", "Riprova Più Tardi (IT) - Try again later"),
    ("azione bloccata", "Azione Bloccata (IT) - Action blocked"),
    ("temporaneamente bloccato", "Temporaneamente Bloccato (IT) - Temporarily blocked"),
    ("troppo veloce", "Troppo Veloce (IT) - Going too fast"),
    ("limite superato", "Limite Superato (IT) - Limit exceeded"),
    ("attendi", "Attendi (IT) - Instagram is throttling you"),
)
