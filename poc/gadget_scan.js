// Frida gadget scanner used to recover the app's plaintext device_command body.
// Attach to the gadget (e.g. frida -H 127.0.0.1:27042 -n Gadget -l gadget_scan.js).
// It is read-only: it scans rw- memory for the plan marker and prints the JSON.

var PAT = '70 65 61 6b 5f 76 61 6c 6c 65 79 5f 70 72 69 63 65 73'; // "peak_valley_prices"
var busy = false;

function scan() {
  if (busy) return;
  busy = true;
  try {
    Process.enumerateRanges('rw-').forEach(function (r) {
      if (r.size > 150 * 1024 * 1024) return;
      try {
        Memory.scanSync(r.base, r.size, PAT).forEach(function (h) {
          try { send({ addr: String(h.address), data: h.address.readUtf8String(4000) }); } catch (e) {}
        });
      } catch (e) {}
    });
  } catch (e) { send({ err: String(e) }); }
  busy = false;
}

setInterval(scan, 2500);
send({ tag: 'scanner-ready' });
