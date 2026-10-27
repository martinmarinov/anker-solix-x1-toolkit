# Continuing the reverse engineering (LLM/agent playbook)

This document is for a future LLM or developer adding functionality to this project.
It assumes you have a real Anker SOLIX X1, an Android phone, and a Mac/Linux host.

Read [`API.md`](API.md) first. Everything below is about **discovering** new calls safely.

## Ground rules

1. **Use your own account and system.** Never test against someone else's site.
2. **Read before write.** Prefer read-only probes; snapshot the plan before any write.
3. **One change at a time.** Change one field, read it back, restore.
4. **Never publish** tokens, certificates, serials, site UUIDs or account ids.
5. **Do not use the Dynamic Tariff scheduler** for testing on a live battery: it is
   window-driven and has caused overcurrent trips. The TOU path is target-driven.
6. Keep a rollback: `get_device_command` output is a complete restorable snapshot.

## Toolchain

```sh
brew install apktool mitmproxy android-platform-tools
python3 -m pip install --user frida-tools paho-mqtt requests cryptography
# blutter for Flutter AOT (see its README); jadx for Java/smali inspection
```

Useful inputs:

- APK splits (`base.apk`, `split_config.arm64_v8a.apk`, …)
- `libapp.so` — Flutter AOT; `strings` gives endpoints, field names, `action_*` commands
- `classes.dex` — often a small packed shell; the real Java is decrypted at runtime

## Static recon

```sh
apktool d base.apk -o decoded
strings -a lib/arm64-v8a/libapp.so > libapp.str.txt
rg -o '/[a-z0-9_/]*(rate_plan|device_command|mqtt|price)[a-z0-9_/]*' libapp.str.txt | sort -u
```

Then use `blutter` to recover Dart class/function structure:

```sh
blutter lib/arm64-v8a/libapp.so .
# produces asm/**, pp.txt
rg -n 'get_utility_rate_plans|PeakValleyPrices|toJson' asm
```

Look for the model class that serialises the feature (`toJson`) and the HTTP service
that posts it. The app is not symbol-obfuscated at the Dart level; file paths survive.

## HTTP capture with mitmproxy

Android 7+ apps trust **system CAs only**. A user CA is ignored unless the app opts in.
For this app the reliable route is a **re-signed debug build** with:

```xml
<application android:debuggable="true"
             android:networkSecurityConfig="@xml/nsc_mitm" ...>
```

`res/xml/nsc_mitm.xml`:

```xml
<network-security-config>
  <base-config cleartextTrafficPermitted="true">
    <trust-anchors>
      <certificates src="system"/>
      <certificates src="user"/>
    </trust-anchors>
  </base-config>
</network-security-config>
```

Then:

```sh
adb push ~/.mitmproxy/mitmproxy-ca-cert.cer /sdcard/Download/
# Settings → Security → Encryption & credentials → Install a certificate → CA certificate
adb shell settings put global http_proxy <host>:8080
mitmdump -s captures/capture_live.py --listen-port 8080
```

The addon in `captures/capture_live.py` saves request/response bodies and redacts
`x-auth-token`, `gtoken`, `cookie`, `authorization`.

**The body is app-encrypted.** TLS capture shows base64 ciphertext for most calls. A few
calls accept plain JSON — `device_command`, `get_device_command`,
`get_user_mqtt_info`, `get_utility_rate_plans` — so try a plain request first.

## Recovering the plaintext payload with a Frida gadget

When the body is encrypted and you need the exact JSON, instrument the app.

1. Add `libgadget.so` (Frida android-arm64) and `libgadget.config.so` to
   `lib/arm64-v8a/`, set `android:extractNativeLibs="true"`.
2. Patch the **app component factory** (`s.h.e.l.l.A.instantiateApplication`) to call
   `System.loadLibrary("gadget")`. Patching the Application's `onCreate` is not enough:
   the packed shell delegates to a decrypted Application.
3. The packer's `SecurityCheckThread` may throw a Flutter `@UiThread` violation when the
   gadget is present and kill the process. Install a smali
   `Thread.UncaughtExceptionHandler` that logs and swallows that exception.
4. Rebuild, zipalign, re-sign, install with the original splits.

This gadget build exposes **no Java bridge** (`typeof Java === "undefined"`), and the
request cipher is not OpenSSL. What worked was a native **memory scan**:

```sh
frida -H 127.0.0.1:27042 -n Gadget -l poc/gadget_scan.js
```

`poc/gadget_scan.js` scans `rw-` ranges for the ASCII marker `peak_valley_prices` and
prints 4 KB around each hit. Open the feature screen, make the change, save, and the
plaintext draft appears in the output. It is read-only.

Frida 17 notes:

- `Module.findExportByName` is gone; use `Module.getGlobalExportByName`.
- Hooking `memcpy`/`memmove` on every call will ANR a Flutter UI. Filter by size and first
  byte, or scan memory instead.
- `EVP_EncryptUpdate`/`AES_cbc_encrypt` produced no hits for the request body.

## MQTT telemetry

`POST /v1/openapi/devicemanage/get_user_mqtt_info` returns broker, client certificate and
private key in **plaintext**. Connect with `paho-mqtt`, TLS 8883, mTLS:

```python
client.tls_set(ca_certs=ca, certfile=cert, keyfile=key)
client.subscribe(f"dt/anker_power/{model}/{sn}/#")
```

The EMS frame (`…/troubleshot_info/ems_second`, ~5 s) is JSON whose `payload.trans` is
base64 JSON with `pp/gp/bp/lp`, flow fields and `soc`. See `API.md`.

## Finding a new feature

1. Locate the screen/class in `asm/` (Dart file paths are descriptive).
2. Find the repository/service call and the endpoint string in `libapp.so`.
3. Identify the request model's `toJson`; note every key.
4. Probe read-only with **plain JSON** first. The server's validation errors name the
   first missing/mistyped field — iterate.
5. Snapshot state, make one change, read back after **3–6 s** (the cloud is eventually
   consistent), restore.
6. Check whether the response is plaintext or `{"data":"<base64>"}`. If encrypted, use
   the gadget/memory route above.

## Debugging checklist

| Symptom | Check |
|---|---|
| 401 / `26084` | token kicked by another login; re-authenticate |
| 400 field error | body shape/type; use the server's hint |
| 200 but nothing changed | eventual consistency; poll; or field ignored |
| prices rounded to 1 dp | missing `accuracy:5`, `sn` on read, or minimal body |
| write rolled back | compare `sent` vs `readback` snapshots in `~/.anker-tou/captures` |
| no MQTT messages | wrong model/serial, certs expired, topic typo |
| app crashes after gadget | packer security thread; install exception handler |
| UI ANRs while hooking | too-hot `memcpy` hook; narrow the filter |

## Cleanup

Uninstall the instrumented build, stop mitm/Frida/MQTT, clear the proxy, remove the user
CA and pushed certificates, re-enable mobile data, and reset stay-awake.
