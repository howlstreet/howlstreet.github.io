# Manual News Injections
#
# Drop entries here when the cron's RSS feeds miss something viral
# (e.g. news that breaks on X first via Kobeissi Letter, Watcher Guru,
# Walter Bloomberg). Each entry becomes a draft on the next cron tick.
#
# Format per entry (separator: '---ENTRY---' on its own line):
#
#   ---ENTRY---
#   title: Headline of the story
#   source_url: https://link-to-source-tweet-or-article
#   source: KOBEISSI LETTER       (optional, displayed in queue meta)
#   format: LOUD_HOWL             (optional, default LOUD_HOWL)
#
#   First paragraph of the news body.
#
#   Second paragraph with the numbers / details.
#
#   Third paragraph if there's more.
#
# When you're done with an entry, delete it (or move to a "posted"
# section below the active entries) so the cron stops re-drafting.
#
# NOTE: The cron now scrapes Kobeissi Letter's substack feed directly
# along with Watcher Guru, TLDR Finance, The Transcript, and Bespoke,
# so most viral-finance news should surface automatically. Use this
# file only for stories that those feeds miss.
