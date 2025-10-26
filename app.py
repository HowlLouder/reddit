from concurrent.futures import ThreadPoolExecutor, as_completed

# ... inside run_scrape, after you have subreddit_list, keyword_list:

results_count = 0

for subreddit_name in subreddit_list:
    try:
        subreddit = reddit.subreddit(subreddit_name)

        # 1) Gather matched posts (lightweight pass)
        matched = []
        for post in subreddit.new(limit=scrape.limit):
            title = post.title or ""
            body  = getattr(post, "selftext", "") or ""
            text_all = f"{title} {body}".lower()
            found = [kw for kw in keyword_list if kw in text_all]
            if not found:
                continue

            url = f"https://reddit.com{post.permalink}"
            post_id = getattr(post, "id", None) or url

            # skip duplicates
            if Result.query.filter_by(scrape_id=scrape.id, reddit_post_id=post_id).first():
                continue

            matched.append({
                "title": title,
                "body": body,
                "found": found,
                "url": url,
                "post_id": post_id,
                "author": str(post.author),
                "score": post.score,
                "subreddit_name": subreddit_name
            })

        # 2) Score in parallel (only if AI enabled)
        scored = []
        if scrape.ai_enabled and matched:
            def _score_item(item):
                s, r = ai_score_post(item["title"], item["body"], item["found"], guidance=scrape.ai_guidance)
                item["ai_score"] = s
                item["ai_reason"] = r
                return item

            with ThreadPoolExecutor(max_workers=AI_MAX_CONCURRENCY) as ex:
                futures = [ex.submit(_score_item, m) for m in matched]
                for fut in as_completed(futures):
                    scored.append(fut.result())
        else:
            # AI disabled => just pass through
            for m in matched:
                m["ai_score"] = None
                m["ai_reason"] = "AI disabled for this scrape"
                scored.append(m)

        # 3) Persist & (optionally) send to GHL
        for item in scored:
            result = Result(
                scrape_id=scrape.id,
                title=item["title"],
                author=item["author"],
                subreddit=item["subreddit_name"],
                url=item["url"],
                score=item["score"],
                keywords_found=",".join(item["found"]),
                ai_score=item["ai_score"],
                ai_reasoning=item["ai_reason"],
                reddit_post_id=item["post_id"],
                is_hidden=False
            )
            db.session.add(result)
            results_count += 1

            if scrape.ai_enabled and (item["ai_score"] or 0) >= AI_MIN_SCORE:
                send_to_ghl({
                    'author': item["author"],
                    'url': item["url"],
                    'title': item["title"],
                    'subreddit': item["subreddit_name"],
                    'keywords_found': item["found"]
                })

    except Exception as e:
        log.exception("Error scraping r/%s: %s", subreddit_name, e)
        continue
