"""
Reciproca - follow & unfollow bot, packaged.

The package façade: importing `reciproca` brings up every public name the
monolith had, so `import reciproca as R; R.rank_queue(...)` and
`from reciproca import order_authors_by_staleness` keep working exactly as
they always did.

Import order below is a strict DAG - each module is imported only after the
modules it imports are fully bound on the package. Modules must not import
earlier-unbound modules (that is what "circular import" would look like here),
and every module reads CONFIG and state through `from reciproca import
config, state` + attribute access at call time, never `from reciproca.state
import driver` (which would bind the value at import time).

Modules follow, in order: config (nothing) -> logging_sink (config) -> state
(logging_sink) -> utils (config, state, logging_sink) -> markers, selectors
(standalone) -> hooks (state, logging_sink) -> persistence (config, state,
hooks) -> queue (config, state, persistence, utils) -> semantic (config,
markers, utils) -> unfollow (config, hooks, state, persistence) -> browser
(config, hooks, state, queue, selectors, unfollow) -> follow (browser, queue,
semantic) -> scraping (browser, follow) -> cycles (everything) -> gui
(everything, tkinter) - then the startup queue validation, as the monolith
ran it.
"""

from . import config
from . import logging_sink
from . import state
from . import utils
from . import markers
from . import selectors
from . import hooks
from . import persistence
from . import queue
from . import semantic
from . import unfollow
from . import browser
from . import follow
from . import scraping
from . import cycles
from . import gui

# ---------------------------
# Façade re-exports - the monolith's public API, module by module
# ---------------------------

# config
from .config import (
    AUTHORS_FILE,
    CONFIG,
    CONFIG_FILE,
    FOLLOWED_FILE,
    FREQUENCIES_FILE,
    HASHTAGS_FILE,
    LOG_FILE,
    QUEUE_FILE,
    ACCOUNT_USERNAME_FILE,
    CHROME_PROFILE_DIR,
    STOP_FLAG_FILE,
    UNFOLLOW_PROGRESS_FILE,
    UNFOLLOW_SESSION_FILE,
    app_dir,
    data_path,
    load_config,
    save_config,
)

# markers
from .markers import (
    CLOSE_BUTTON_LABELS,
    FOLLOWED_SIGNAL_MARKERS,
    FOLLOW_BUTTON_MARKERS,
    FOLLOWERS_LABEL_MARKERS,
    FOLLOWING_BUTTON_MARKERS,
    FOLLOWING_LABEL_MARKERS,
    MUTUAL_FOLLOWERS_MARKERS,
    POSTS_LABEL_MARKERS,
    PROFILE_BUTTON_LABELS,
    RATE_LIMIT_MARKERS,
    UNFOLLOW_CONFIRM_MARKERS,
)

# selectors
from .selectors import (
    EXTRACT_FOLLOWERS_JS,
    LOGIN_USERNAME_SELECTORS,
    LOGIN_USERNAME_XPATHS,
    POST_LINKS_JS,
    PROFILE_STATS_JS,
)

# logging_sink
from .logging_sink import (
    RECENT,
    clear_sinks,
    log,
    logger,
    register_sink,
)

# utils
from .utils import (
    COUNT_CHARS,
    COUNT_AGREEMENT_TOLERANCE,
    author_rejection_reason,
    bot_rejection_reason,
    brief_error,
    count_from_links,
    count_link_value,
    counts_agree,
    has_marker,
    is_follow_button,
    parse_count,
    parse_follower_count,
    parse_labelled_count,
    pause,
    retry,
    validate_number,
    wait_for_clickable,
    wait_for_element,
)

# hooks - the delegators. Where a name also exists in gui (the widget
# implementation), the hooks delegator is the one the core calls, so it is the
# one the façade exposes.
from .hooks import (
    attach,
    detach,
    notify_user,
    on_stop_clicked,
    refresh_queue_display,
    reset_progress,
    reset_unfollow_progress,
    set_progress_maximum,
    stats_handler,
    unfollow_stats_handler,
    update_account_label,
    update_follow_ui_state,
    update_live_extraction_display,
    update_progress,
    update_stats_display,
    update_unfollow_progress,
    update_unfollow_stats_display,
    update_unfollow_ui_state,
)

# persistence
from .persistence import (
    current_account_id,
    is_already_followed,
    load_account_username,
    load_author_history,
    load_frequencies,
    load_hashtags,
    log_followed_user,
    order_authors_by_staleness,
    save_author_history,
    save_frequencies,
    save_hashtags,
    save_login_username,
    uf_get_username,
    uf_load_followers,
    uf_load_following,
    uf_load_progress,
    uf_load_session,
    uf_progress_archive,
    uf_save_progress,
    uf_save_session,
)

# queue
from .queue import (
    FREQUENCY_HALFWAY,
    add_to_queue,
    clear_queue,
    combined_rank,
    frequency_score,
    load_queue,
    queue_affinity,
    queue_username,
    ranking_frequencies,
    rank_queue,
    remove_from_queue,
    save_queue,
    scoring_shortlist,
    score_queue,
    tie_breaker,
    trim_queue,
    validate_queue,
    with_affinity,
)

# semantic
from .semantic import (
    MODEL_DIR,
    MODEL_FILES,
    MODEL_MAX_TOKENS,
    MODEL_REPO,
    MODEL_REVISION,
    SemanticModel,
    affinity_between,
    cosine,
    download_model,
    file_digest,
    make_affinity_scorer,
    mean_pooled,
    model_file,
    model_files_unchanged,
    model_is_downloaded,
    normalized,
    profile_description,
    profile_text,
    semantic_model,
)

# unfollow
from .unfollow import (
    reset_unfollow_state,
    uf_auto_load_last_session,
    uf_check_account,
    uf_load_json_pair,
    unfollow_from_list,
    unfollow_progress_counts,
    unfollow_user,
)

# browser
from .browser import (
    BROWSER_WATCH_INTERVAL,
    begin_session,
    browser_is_open,
    can_open_browser,
    check_rate_limit,
    chrome_options,
    end_session,
    handle_browser_closed,
    login_username_field,
    open_browser,
    poll_browser,
    read_login_username,
    refresh_browser_state,
    start_browser,
    stop_bot,
    watch_login_username,
)

# follow
from .follow import (
    find_follow_button,
    follow_author,
    follow_from_queue,
    follow_user,
    get_button_text,
    profile_bot_reason,
    read_candidate_profile,
    read_profile_stats,
    read_profile_stats_retried,
    run_scoring_pass,
    validate_follow_success,
)

# scraping
from .scraping import (
    close_post,
    collect_post_links,
    extract_users_from_followers,
    get_author_profile,
    leave_extra_window,
    open_followers_popup,
    open_post,
    post_dialog_open,
    scrape_and_fill_queue,
)

# cycles
from .cycles import follow_cycle, unfollow_cycle

# gui - the window itself and its direct handlers. Names that also live in
# hooks (update_progress and friends) are deliberately NOT re-exported from
# here: the core dispatches through the hooks delegators.
from .gui import (
    ToolTip,
    add_hashtag,
    add_scraped_to_queue,
    add_to_queue_ui,
    clear_hashtags,
    clear_queue_ui,
    export_logs,
    export_queue_to_file,
    follow_logic,
    import_queue_from_file,
    main,
    on_closing,
    remove_from_queue_ui,
    remove_hashtag,
    reset_unfollow_app,
    run_follow,
    run_unfollow,
    setup_gui,
    show_about,
    uf_load_json_files,
    unfollow_logic,
    validate_number,
    watch_browser,
)

# Names the monolith's module namespace carried (imports at its top), which
# tests and callers reach through the module: the collections Counter, and the
# two Selenium names the core's callers use.
from collections import Counter
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

# Startup queue validation, exactly as the monolith ran it on launch.
validate_queue()
