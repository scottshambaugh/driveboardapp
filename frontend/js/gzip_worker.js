// gzip a job off the ui thread, so a big upload does not freeze the interface
//
// takes either a File or a job string and posts back the gzipped bytes as a
// transferable ArrayBuffer

importScripts("pako_deflate.min.js");

onmessage = function (e) {
  var job = e.data;
  var data;
  if (typeof job === "string") {
    data = job;
  } else {
    // reading the file here keeps the whole string off the main thread
    data = new Uint8Array(new FileReaderSync().readAsArrayBuffer(job));
  }
  // level 1 keeps nearly all of the size win for two thirds of the cpu time
  var out = pako.gzip(data, { level: 1 });
  postMessage(out.buffer, [out.buffer]);
};
