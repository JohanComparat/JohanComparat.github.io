# johancomparat.github.io

Personal research website of Johan Comparat, served by GitHub Pages at https://johancomparat.github.io.
It is built with Jekyll from the [Academic Pages](https://github.com/academicpages/academicpages.github.io) template.

## Content

| Page | Source |
|---|---|
| Home | `_pages/about.md` |
| Publications | `_pages/publications.html`, rendering `_data/publications.yml` (generated from NASA ADS) |
| Talks | `_pages/talks.html`, rendering `_data/talks.yml` |
| Teaching | `_pages/teaching.md` |
| Habilitation | `_pages/hdr.md` |
| CV | `_pages/cv.md` |

Site settings and sidebar links are in `_config.yml`, and the menu is in `_data/navigation.yml`.

## Updating the publication list

The list is fetched from NASA ADS with the query `author:"Comparat, Johan"`.
`scripts/pub_sections.yml` assigns each bibcode to a section of the page.

```bash
export ADS_API_TOKEN=...   # https://ui.adsabs.harvard.edu/user/settings/token
python3 scripts/ads_publications.py
```

Refereed articles missing from `scripts/pub_sections.yml` are classified automatically: first-author papers go to "First author" and the others to "Recent articles". Non-refereed records missing from it are left out. The script lists both kinds of record so you can move them into the right section of `scripts/pub_sections.yml`; list a bibcode under `exclude` to hide it. Then run the script again and commit `_data/publications.yml`.

## Building locally

```bash
sudo apt install ruby-dev ruby-bundler
bundle config set --local path vendor/bundle
bundle install
bundle exec jekyll serve -l -H localhost   # http://localhost:4000
```
