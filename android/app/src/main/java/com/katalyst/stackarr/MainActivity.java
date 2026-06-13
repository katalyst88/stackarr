package com.katalyst.stackarr;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

/**
 * Stackarr Android client — a thin, configurable WebView wrapper around a
 * self-hosted Stackarr instance. On first launch it asks for the server URL
 * (Stackarr is self-hosted, so it can't be baked in) and remembers it.
 * An alternative to installing the PWA.
 */
public class MainActivity extends Activity {

    private WebView web;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("stackarr", MODE_PRIVATE);

        web = new WebView(this);
        setContentView(web);

        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());

        String url = prefs.getString("url", null);
        if (url == null || url.isEmpty()) {
            promptUrl();
        } else {
            web.loadUrl(url);
        }
    }

    private void promptUrl() {
        final EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        in.setHint("http://192.168.1.10:8484");
        in.setText(prefs.getString("url", ""));
        new AlertDialog.Builder(this)
                .setTitle("Stackarr server")
                .setMessage("Enter the address of your Stackarr instance.")
                .setView(in)
                .setCancelable(false)
                .setPositiveButton("Connect", (dialog, which) -> {
                    String u = in.getText().toString().trim();
                    if (!u.startsWith("http://") && !u.startsWith("https://")) {
                        u = "http://" + u;
                    }
                    prefs.edit().putString("url", u).apply();
                    web.loadUrl(u);
                })
                .show();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Change server");
        menu.add(0, 2, 0, "Reload");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case 1:
                promptUrl();
                return true;
            case 2:
                web.reload();
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }
}
