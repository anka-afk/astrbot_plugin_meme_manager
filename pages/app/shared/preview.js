// HTTP responses provide persistent caching even inside AstrBot's sandboxed iframe.
export class PreviewClient {
  constructor(api) {
    this.api = api;
    this.limit = 2;
    this.active = 0;
    this.queue = [];
    this.manifests = new Map();
    this.requests = new Map();
    this.cache = new Map();
    this.cacheBytes = 0;
  }

  invalidate() {
    this.manifests.clear();
  }

  async get(
    endpoint,
    params,
    { isCurrent = () => true, priority = false } = {},
  ) {
    const scope = String(params.managed_pack_id || params.pack_id || "");
    let manifestRequest = this.manifests.get(scope);
    if (!manifestRequest) {
      manifestRequest = this.api.apiGet("preview/manifest", {
        managed_pack_id: scope,
      });
      this.manifests.set(scope, manifestRequest);
    }
    let manifest;
    try {
      manifest = await manifestRequest;
    } catch (error) {
      if (this.manifests.get(scope) === manifestRequest)
        this.manifests.delete(scope);
      throw error;
    }
    if (!isCurrent())
      throw new DOMException("Preview no longer visible", "AbortError");
    const configured = manifest.concurrency;
    this.limit =
      Number.isInteger(configured) && configured >= 1 && configured <= 8
        ? configured
        : 2;
    const version =
      manifest.versions?.[`${params.category}/${params.filename}`];
    const requestParams = { ...params };
    if (endpoint === "meme_image_data") {
      requestParams.managed_pack_id = manifest.pack_id;
      if (version) requestParams.v = version;
    }
    const key = JSON.stringify([
      endpoint,
      Object.entries(requestParams).sort(),
    ]);
    const cached = this.cache.get(key);
    if (cached) {
      this.cache.delete(key);
      this.cache.set(key, cached);
      return cached;
    }
    let job = this.requests.get(key);
    if (job) {
      job.consumers.push(isCurrent);
      if (priority && this.queue.includes(job)) {
        this.queue.splice(this.queue.indexOf(job), 1);
        this.queue.unshift(job);
      }
    } else {
      job = {
        endpoint,
        params: requestParams,
        key,
        scope,
        consumers: [isCurrent],
      };
      job.promise = new Promise((resolve, reject) => {
        job.resolve = resolve;
        job.reject = reject;
      });
      this.requests.set(key, job);
      if (priority) this.queue.unshift(job);
      else this.queue.push(job);
    }
    this.pump();
    const result = await job.promise;
    if (!isCurrent())
      throw new DOMException("Preview no longer visible", "AbortError");
    return result;
  }

  pump() {
    while (this.active < this.limit && this.queue.length) {
      const job = this.queue.shift();
      if (!job.consumers.some((current) => current())) {
        this.requests.delete(job.key);
        job.reject(new DOMException("Preview no longer visible", "AbortError"));
        continue;
      }
      this.active += 1;
      // Keep the slot occupied until the bridge request actually settles.
      Promise.resolve()
        .then(() => this.api.apiGet(job.endpoint, job.params))
        .then((data) => {
          if (!data?.data_url) throw new Error("Missing preview image data");
          if (job.params.v && job.params.size !== "original") {
            const bytes = data.data_url.length * 2;
            const budget = 16 * 1024 * 1024;
            if (bytes <= budget) {
              while (this.cacheBytes + bytes > budget && this.cache.size) {
                const oldest = this.cache.keys().next().value;
                this.cacheBytes -= this.cache.get(oldest).data_url.length * 2;
                this.cache.delete(oldest);
              }
              this.cache.set(job.key, data);
              this.cacheBytes += bytes;
            }
          }
          job.resolve(data);
        })
        .catch((error) => {
          this.manifests.delete(job.scope);
          job.reject(error);
        })
        .finally(() => {
          this.requests.delete(job.key);
          this.active -= 1;
          this.pump();
        });
    }
  }
}
