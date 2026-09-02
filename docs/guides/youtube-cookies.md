# YouTube `cookies.txt` Setup

If YouTube restricts downloads or requires sign-in, you can provide a
`cookies.txt` file to fnack. The file is owned by the YouTube downloader
plugin (managed from **Settings → Plugins** — never a core setting).

## Step-by-step

1. **Install a cookie exporter extension**:
   - Chrome: **Get cookies.txt LOCALLY** from the
     [Chrome Web Store](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox: the same extension from
     [Firefox Add-ons](https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt-locally/)
2. **Log in to YouTube**: open [youtube.com](https://www.youtube.com) (or
   [music.youtube.com](https://music.youtube.com)) and make sure you are
   signed in.
3. **Export cookies**: click the extension icon on the YouTube tab, then
   **Export** / **Export As cookies.txt**, and save the file to your computer.
4. **Apply in fnack**:
   - **Method A (web UI)**: go to **Settings → YouTube Cookies (cookies.txt)**,
     select the exported file, and click **Upload File** (or paste the content
     directly).
   - **Method B (Docker mount)**: place the file in your host config directory
     (e.g. `./config/cookies.txt`), which is mounted to `/config/cookies.txt`
     inside the container.

> `cookies.txt` is never baked into the image and is excluded from any release
> bundle — it lives only on your host volume.
