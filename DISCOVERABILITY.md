# Search discovery changes

## Implemented

- Product detail pages now serve their own content instead of redirecting to Amazon.
  Previously 109 of 198 sitemap URLs had an external redirect configured; the live
  Blue Pure product URL returned that redirect during the audit.
- The seven older product permalinks and their `/product/` aliases permanently
  redirect to the corresponding `/products/` page. Canonicals, internal search,
  and sitemaps use the consolidated destination.
- Product cards link their titles and images to actual generated detail pages.
  Separate Amazon buttons retain the existing affiliate tag and `rel="sponsored"`.
- The 95 imported catalog entries without descriptions are browsable but marked
  `noindex,follow` and excluded from both XML sitemaps. They should only become
  indexable after substantive, accurate product content is added to the source.
  The seven products with descriptions remain indexable.
- Product structured data no longer invents stock availability or identifies this
  publisher as the manufacturer. No prices, ratings, or testing results were added.
  Basic Product markup alone does not qualify a page for Google's product rich results.
- Home and catalog titles, descriptions, and headings explain what readers can find.
  Shopping content includes a visible affiliate disclosure, and the footer links
  to the buying guide, contact, disclosure, and privacy pages.
- Search and AI referral crawlers are explicitly allowed in the origin robots file.
  Cloudflare's managed training rules remain a separate configuration.
- The builder preserves unrelated project files and supports a separate output directory.
  `python3 validate_site.py` checks the generated routes, sitemaps, links, JSON-LD,
  affiliate attributes, and origin search crawler access.

## Deployment and Cloudflare checks

These changes must be deployed through the existing Cloudflare Pages project to
affect the live site. No Cloudflare account settings are changed by this repository.

1. Deploy the regenerated static files, including `_redirects`, `robots.txt`, and
   both sitemaps, along with `assets/styles.css`.
2. Confirm `/products/levoit-core-300-air-purifier/` returns 200 and the older
   `/levoit-core-300-air-purifier/` permanently redirects there, rather than to Amazon.
3. Inspect the served robots file after Cloudflare prepends its managed block.
   In Cloudflare's AI Crawler and Agent access policies, allow Search and Agent
   access if referral discovery is desired; training can remain restricted.
   Review AI Crawl Control/WAF events for legitimate search crawlers receiving blocks
   or challenges. An origin `Allow` rule does not override a firewall decision.
4. The observed managed block disallows Baiduspider and PetalBot. Review those
   entries in Cloudflare if those search audiences matter; prefer a consistent
   served policy rather than depending on different parsers to merge conflicting rules.
5. Submit `https://airpurify.ing/sitemap.xml` and
   `https://airpurify.ing/sitemap-images.xml` in Google Search Console and Bing
   Webmaster Tools. Inspect a product, article, and recommendation URL after deployment.

## Editorial work that needs evidence

Improve the existing reviews with verified model specifications, dated sources,
clear comparisons, limitations, and real author information. Identify hands-on
testing only where it actually occurred. Update older dated buying guides only
after researching their recommendations; do not change publication dates merely
to make them look fresh. Replace title-only catalog entries with useful original
content before allowing them into search. Monitor impressions, indexed URLs,
referral visits, and affiliate conversions after deployment; indexing and traffic
gains cannot be established by a local validation run.

## References

- [Google: site navigation and internal product links](https://developers.google.com/search/docs/specialty/ecommerce/help-google-understand-your-ecommerce-site-structure)
- [Google: structured data accuracy](https://developers.google.com/search/docs/appearance/structured-data/sd-policies)
- [Google: useful product reviews](https://developers.google.com/search/docs/specialty/ecommerce/write-high-quality-reviews)
- [Cloudflare: managed robots.txt](https://developers.cloudflare.com/bots/additional-configurations/managed-robots-txt/)
- [OpenAI: search and training crawlers](https://developers.openai.com/api/docs/bots)
- [Anthropic: separate search, user, and training crawlers](https://support.claude.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler)
