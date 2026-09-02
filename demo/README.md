# Demo

Drop your demo video in this folder.

`.gitignore` excludes `*.mp4` everywhere else in the repository — recordings and
rendered output are far too large to track — but it makes an explicit exception
for `demo/*.mp4`, so anything you put here **will** be committed.

## Suggested

A short real-time clip, ideally the one that shows both amplitude clusters:

```bash
python ../chirp/dat_to_video.py --folder /path/to/recording amp-A-010.dat --duration 10
```

Then reference it from the top-level `README.md`:

```markdown
https://github.com/JesusJBallesteros/CHIRP/assets/<id>/<name>.mp4
```

GitHub renders an inline player for a video **attached through the web
interface** (drag it into an issue or a README edit box and use the URL it
generates). A plain relative link such as `demo/demo.mp4` renders only as a
download link, not a player — so upload it that way if you want it to play in
the README.

## Size

Keep it under ~10 MB if you can. GitHub warns above 50 MB and hard-refuses at
100 MB, and every clone pays for it forever. A 10 s clip at 1600×900 comes out
around 1 MB at the default CRF, so this is rarely a problem.

If you ever do need something bigger, attach it to a
[Release](https://github.com/JesusJBallesteros/CHIRP/releases) instead of
committing it — release assets do not bloat the clone.
