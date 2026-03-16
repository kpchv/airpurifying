# airpurify.ing static site

This repository contains a static rebuild of the original WordPress site content and media.

## What was extracted

- Published WordPress pages/posts/products from the SQL export
- Product affiliate links from WooCommerce external product metadata
- Additional product feed records from `_cegg_data_Amazon` metadata
- Logo and media assets from `uploads/`
- SEO index files (`sitemap.xml`, `robots.txt`) and Cloudflare Pages files (`_redirects`, `_headers`)

## Regenerate site

```bash
python3 build_site.py
```

## Deploy to Cloudflare Pages

- Build command: *(none)*
- Build output directory: `/`
- Root directory: repository root

The project is fully static and ready for GitHub + Cloudflare Pages deployment.
