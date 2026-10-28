const TARGETS = [
  ['cmd',   '64 65 76 69 63 65 5f 63 6f 6d 6d 61 6e 64'],       // device_command
  ['limit', '61 63 5f 70 6f 77 65 72 5f 6c 69 6d 69 74'],       // ac_power_limit
  ['attrs', '73 65 74 5f 64 65 76 69 63 65 5f 61 74 74 72 73'], // set_device_attrs
];
let busy = false;
function scan() {
  if (busy) return; busy = true;
  try {
    Process.enumerateRanges('rw-').forEach(function (r) {
      if (r.size > 150 * 1024 * 1024) return;
      TARGETS.forEach(function (t) {
        try {
          Memory.scanSync(r.base, r.size, t[1]).forEach(function (h) {
            try {
              let start = h.address.sub(2048);
              let data = '';
              try { data = start.readUtf8String(12288); }
              catch (e) { data = h.address.readUtf8String(12288); }
              if (data) send({ tag: t[0], addr: String(h.address), data: data });
            } catch (e) {}
          });
        } catch (e) {}
      });
    });
  } catch (e) {}
  busy = false;
}
setInterval(scan, 750);
scan();
send({ tag: 'ready', data: String(Date.now()) });
