// Exercises EXTRACT_FOLLOWERS_JS from reciproca/selectors.py against a synthetic
// followers dialog. Reads the real source out of the Python file so the test
// cannot drift from the shipped code.
const fs = require('fs');
const { JSDOM } = require('jsdom');

const py = fs.readFileSync(require('path').join(__dirname, '..', 'reciproca', 'selectors.py'), 'utf8');
const start = py.indexOf('EXTRACT_FOLLOWERS_JS = r"""') + 'EXTRACT_FOLLOWERS_JS = r"""'.length;
const end = py.indexOf('"""', start);
const SCRIPT = py.slice(start, end);

// Same values as FOLLOWING_BUTTON_MARKERS in reciproca/markers.py
const MARKERS = ['following', 'requested', 'segui già', 'seguendo', 'richiesta', 'in attesa'];

// Selenium runs the script as a function body with arguments; mirror that.
const run = (dom, markers) => {
  const g = dom.window;
  const fn = new Function('document', 'return (function(){' + SCRIPT + '}).apply(null, [].slice.call(arguments, 1))');
  return fn(g.document, markers);
};

let failures = 0;
function check(name, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failures++;
  console.log(`${ok ? 'OK  ' : 'FAIL'} ${name}`);
  if (!ok) console.log(`       got      ${a}\n       expected ${e}`);
}

// ---- Fixture 1: realistic dialog, mixed button states -------------------
// Two profile links per row (avatar + name), exactly like Instagram.
function row(user, buttonText) {
  const btn = buttonText === null ? '' : `<button>${buttonText}</button>`;
  return `
    <div class="row">
      <div><a href="/${user}/"><img></a></div>
      <div><a href="/${user}/"><span>${user}</span></a></div>
      ${btn}
    </div>`;
}

const dom1 = new JSDOM(`<div role="dialog"><div class="scroller">
  ${row('mario', 'Segui già')}
  ${row('luigi', 'Segui')}
  ${row('anna', 'Following')}
  ${row('paolo', 'Follow')}
  ${row('giulia', 'Richiesta')}
</div></div>`);
const r1 = run(dom1, MARKERS);
check('mixed rows: keeps only not-followed', r1.kept, ['luigi', 'paolo']);
check('mixed rows: counts skipped', r1.skippedFollowing, 3);
check('mixed rows: all buttons readable', r1.rowsWithoutButton, 0);
check('mixed rows: rows inspected', r1.rowsInspected, 5);

// ---- Fixture 2: the cross-contamination trap ----------------------------
// A followed user immediately before a not-followed one. If the row walk goes
// one level too far it reads the neighbour's button and wrongly skips.
const dom2 = new JSDOM(`<div role="dialog"><div>
  ${row('followed_one', 'Segui già')}
  ${row('fresh_one', 'Segui')}
</div></div>`);
check('no cross-contamination between adjacent rows', run(dom2, MARKERS).kept, ['fresh_one']);

// ---- Fixture 3: deeper nesting, button further from the link ------------
const dom3 = new JSDOM(`<div role="dialog"><div><div><div>
  <div class="row">
    <div><div><div><a href="/deep_user/"><span>deep_user</span></a></div></div></div>
    <div><div><button>Segui già</button></div></div>
  </div>
  <div class="row">
    <div><div><div><a href="/deep_fresh/"><span>deep_fresh</span></a></div></div></div>
    <div><div><button>Segui</button></div></div>
  </div>
</div></div></div>`);
const r3 = run(dom3, MARKERS);
check('deep nesting: resolves correct button', r3.kept, ['deep_fresh']);
check('deep nesting: no unreadable rows', r3.rowsWithoutButton, 0);

// ---- Fixture 4: row without any button (fail-open) ----------------------
const dom4 = new JSDOM(`<div role="dialog"><div>
  ${row('no_button_user', null)}
  ${row('normal_user', 'Segui già')}
</div></div>`);
const r4 = run(dom4, MARKERS);
check('missing button: user kept (fail open)', r4.kept, ['no_button_user']);
check('missing button: counted for diagnostics', r4.rowsWithoutButton, 1);

// ---- Fixture 5: total breakage -> fail open, fully flagged --------------
const dom5 = new JSDOM(`<div role="dialog"><div>
  ${row('a_user', null)}
  ${row('b_user', null)}
</div></div>`);
const r5 = run(dom5, MARKERS);
check('layout change: nobody dropped', r5.kept, ['a_user', 'b_user']);
check('layout change: flagged as all-unreadable', [r5.rowsWithoutButton, r5.rowsInspected], [2, 2]);

// ---- Fixture 6: junk links are ignored ----------------------------------
const dom6 = new JSDOM(`<div role="dialog"><div>
  <a href="/explore/">explore</a>
  <a href="/p/abc123/">a post</a>
  <a href="/reels/">reels</a>
  ${row('real_user', 'Segui')}
</div></div>`);
check('reserved/non-profile links ignored', run(dom6, MARKERS).kept, ['real_user']);

// ---- Fixture 7: English locale ------------------------------------------
const dom7 = new JSDOM(`<div role="dialog"><div>
  ${row('en_followed', 'Following')}
  ${row('en_requested', 'Requested')}
  ${row('en_fresh', 'Follow')}
</div></div>`);
check('English UI handled', run(dom7, MARKERS).kept, ['en_fresh']);

// ---- Fixture 8: no dialog at all ----------------------------------------
const dom8 = new JSDOM(`<div>nothing here</div>`);
check('no dialog: empty result', run(dom8, MARKERS).kept, []);

console.log(failures === 0 ? '\nALL TESTS PASSED' : `\n${failures} TEST(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
