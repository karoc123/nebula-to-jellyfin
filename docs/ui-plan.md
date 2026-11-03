# UI redesign plan

## Direction

Build a compact, mobile-first download manager. Keep the dark theme, English UI
text, existing functionality and persistent data. Use the existing server-rendered
templates and vanilla JavaScript; no framework or schema changes are needed.

## Information hierarchy

1. Downloads: current work, queue state, search and status filters, download list.
2. Channels: subscriptions, check results, catalog selection.
3. Activity: import/check/test jobs and recovery actions.
4. Settings: authentication, schedule, advanced download options, updates.
5. Logs: diagnostics with readable output and explicit refresh state.

## Shared design

- Mobile layout is the default; expand into columns on larger screens.
- Compact content with touch controls at least 44px tall, visible keyboard focus,
  accessible labels, sufficient contrast and reduced-motion support.
- Restrained surfaces, consistent spacing and typography, one clear primary action.
- Header navigation must fit narrow screens without page overflow. All five pages
  remain reachable. Cookie storage status must not imply verified authentication.
- Text labels accompany status colors. Long titles, errors and URLs wrap safely.
- Feedback appears near its action; global failures remain visible and announced.

## Downloads and import

- Highlight current downloading/publishing work independently of list filters using
  existing API data. Show progress with a numeric label; never invent ETA or speed.
- Keep pause/resume and pause reason clear. Explain that pausing affects the queue.
- Search, status chips with counts, and channel filter precede compact responsive
  rows. Put all existing bulk actions in a clearly labelled disclosure, preserving
  their channel/search scope and confirmation counts.
- Distinguish an empty queue from an empty filtered result; offer import or reset.
- Add downloads opens the existing inline import section. Preserve multi-URL video
  and channel import. Separate video/channel selection; show latest count only for
  latest mode and subscription only for channel mode. Explain future-only baseline.
- Keep the existing asynchronous import redirect to Activity, with clear success
  copy and a route back to Downloads. Do not expand API contracts unnecessarily.
- Remove from list explains that finished library files remain intact.

## Channels and catalog

- Compact channel cards with name, subscription, downloaded count, failures and
  last check. Add client-side search and subscription/error filters.
- Retain all subscribe/check/catalog actions and next scheduled check.
- Catalog rows work on phones; selection count and download action are clear,
  select-all state stays accurate, and unavailable rows remain disabled.

## Settings, Activity and Logs

- Authentication comes first; saved cookies and verified validity remain distinct.
  Preserve file/paste import, empty-input removal semantics and cookie tests.
- Schedule and advanced JSON options remain in the same save form to preserve
  current atomic settings submission. Put JSON in a labelled advanced disclosure.
- Updates distinguish unknown checks from available updates and actual errors.
- Activity retains pagination, retry and clear actions, with readable job results.
- Logs retain automatic refresh and scroll behavior, with visible loading/error
  state. Do not expose cookie contents or sensitive configuration.

## Implementation ownership

- Terra foundation agent: base.html and style.css only; shared design and navigation.
- Terra workflow agent: other templates, app.js, logs.js and focused web tests;
  all page behavior and accessible markup. Coordinate shared class conventions.
- Parent: integration, focused repairs and final verification. No commits or PRs.

## Acceptance and verification

- All existing functionality remains reachable and all existing tests pass.
- Verify rendered pages and import modes with focused regression checks where
  behavior changes. Check JavaScript syntax and run Ruff and pytest.
- Check mobile sizing constraints in CSS, keyboard/focus behavior in code, and
  interaction state preservation during polling (focus, open errors, selections).
- Do not claim browser verification unless it was actually performed.
- Leave pre-existing workflow, Dockerfile and lockfile modifications untouched.

## Implementation result

Both Terra implementation tasks are complete. The parent integrated the shared
styles and workflows, added the current-work progress bar, preserved keyboard
focus when rows refresh or disappear, and disabled bulk actions while new filters
load. Schedule controls precede the advanced options disclosure.

Validation: 138 full-suite tests passed; 40 web tests passed after final integration
adjustments. Ruff lint/format, JavaScript syntax and whitespace checks passed.
A focused Node check verified focus restoration and fallback after row removal.
Browser layout and touch interaction verification remain outstanding.
