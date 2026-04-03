function waxidApp() {
  return {
    // View state
    view: 'nowplaying',
    libraryView: 'grid',

    // Now playing
    nowPlaying: null,
    lastPlaying: null,
    eventSource: null,
    elapsedTimer: null,
    displayElapsed: null,

    // Android client
    isAndroidClient: /WaxID-Android/.test(navigator.userAgent),
    clientListening: false,

    // Library
    albums: [],
    searchQuery: '',
    sortField: 'artist',
    sortAsc: true,

    // Album detail
    albumDetail: null,
    editForm: {},
    editMessage: '',
    deletingTracks: new Set(),
    editingTrack: null,
    trackEditForm: {},

    // Add tracks to album
    addingTracks: false,
    addTracksProgress: 0,
    addTracksTotal: 0,

    // Upload
    uploading: false,
    uploadProgress: 0,
    uploadResult: null,
    dragOver: false,

    // Settings
    settingsForm: {
      roon_enabled: false,
      roon_url: '',
      roon_zone_name: 'Record Player',
      server_url: 'http://localhost:8457',
    },
    settingsMessage: '',
    settingsSaving: false,

    init() {
      this.connectSSE();
      this.loadAlbums();
      // Sync listening state from Android bridge
      if (this.isAndroidClient && window.WaxID) {
        this.clientListening = window.WaxID.isListening();
        // Expose setter for Android to push state changes via evaluateJavascript
        window.setClientListening = (v) => { this.clientListening = v; };
        // Fallback poll every 5s
        setInterval(() => {
          this.clientListening = window.WaxID.isListening();
        }, 5000);
      }
      // Manage SSE based on view
      this.$watch('view', (val) => {
        if (val === 'nowplaying') {
          this.connectSSE();
        } else {
          this.disconnectSSE();
        }
        if (val === 'settings') {
          this.loadSettings();
        }
      });
    },

    toggleListening() {
      if (!window.WaxID) return;
      if (this.clientListening) {
        window.WaxID.stopListening();
        this.clientListening = false;
        this.nowPlaying = { status: 'idle' };
      } else {
        window.WaxID.startListening();
        this.clientListening = true;
        this.nowPlaying = { status: 'listening' };
        this._listenStartedAt = Date.now();
      }
    },

    openAppSettings() {
      if (window.WaxID) {
        window.WaxID.openSettings();
      }
    },

    connectSSE() {
      if (this.eventSource) return; // already connected
      this.eventSource = new EventSource('/now-playing/stream');
      this.eventSource.onmessage = (e) => {
        const data = JSON.parse(e.data);
        // Don't let SSE override optimistic "listening" with "idle" right after starting
        if (data?.status === 'idle' && this._listenStartedAt && Date.now() - this._listenStartedAt < 15000) {
          return;
        }
        this._listenStartedAt = null;
        this.nowPlaying = data;
        if (data?.status === 'playing') {
          this.lastPlaying = data;
        } else if (data?.status === 'idle') {
          this.lastPlaying = null;
        }
        if (data?.status === 'playing' && data.elapsed_s != null) {
          this._elapsedBase = data.elapsed_s;
          this._elapsedReceivedAt = Date.now();
          this._startElapsedTimer();
        } else {
          this._stopElapsedTimer();
        }
      };
      this.eventSource.onerror = () => {
        this.disconnectSSE();
        setTimeout(() => {
          if (this.view === 'nowplaying') this.connectSSE();
        }, 3000);
      };
    },

    disconnectSSE() {
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
      this._stopElapsedTimer();
    },

    _startElapsedTimer() {
      this._stopElapsedTimer();
      this.displayElapsed = this._elapsedBase;
      this.elapsedTimer = setInterval(() => {
        if (this._elapsedBase != null && this._elapsedReceivedAt != null) {
          this.displayElapsed = this._elapsedBase + (Date.now() - this._elapsedReceivedAt) / 1000;
        } else {
          this._stopElapsedTimer();
        }
      }, 1000);
    },

    _stopElapsedTimer() {
      if (this.elapsedTimer) {
        clearInterval(this.elapsedTimer);
        this.elapsedTimer = null;
      }
      this.displayElapsed = null;
    },

    async loadAlbums() {
      try {
        const r = await fetch('/albums');
        if (r.ok) {
          this.albums = await r.json();
        }
      } catch (e) {
        console.error('Failed to load albums:', e);
      }
    },

    get filteredAlbums() {
      const q = this.searchQuery.trim().toLowerCase();
      if (!q) return this.albums;
      return this.albums.filter(a => {
        const artist = (a.artist || '').toLowerCase();
        const name = (a.name || '').toLowerCase();
        const year = String(a.year || '');
        return artist.includes(q) || name.includes(q) || year.includes(q);
      });
    },

    get sortedAlbums() {
      return [...this.filteredAlbums].sort((a, b) => {
        let va = a[this.sortField] ?? '';
        let vb = b[this.sortField] ?? '';
        if (typeof va === 'string') {
          va = va.toLowerCase();
          vb = (vb || '').toLowerCase();
        }
        if (va < vb) return this.sortAsc ? -1 : 1;
        if (va > vb) return this.sortAsc ? 1 : -1;
        return 0;
      });
    },

    sortBy(field) {
      if (this.sortField === field) {
        this.sortAsc = !this.sortAsc;
      } else {
        this.sortField = field;
        this.sortAsc = true;
      }
    },

    sortIndicator(field) {
      if (this.sortField !== field) return '';
      return this.sortAsc ? '\u25B2' : '\u25BC';
    },

    async openAlbum(id) {
      try {
        const r = await fetch(`/albums/${id}`);
        if (!r.ok) return;
        this.albumDetail = await r.json();
        this.editForm = {
          artist: this.albumDetail.artist,
          name: this.albumDetail.name,
          year: this.albumDetail.year,
          discogs_url: this.albumDetail.discogs_url,
        };
        this.editMessage = '';
        this.deletingTracks = new Set();
        this.editingTrack = null;
        this.view = 'album-detail';
      } catch (e) {
        console.error('Failed to open album:', e);
      }
    },

    async saveAlbum() {
      const oldDiscogsUrl = this.albumDetail.discogs_url || '';
      const newDiscogsUrl = this.editForm.discogs_url || '';
      try {
        const r = await fetch(`/albums/${this.albumDetail.album_id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.editForm),
        });
        if (r.ok) {
          this.editMessage = 'Saved!';
          await this.openAlbum(this.albumDetail.album_id);
          await this.loadAlbums();
          if (newDiscogsUrl && newDiscogsUrl !== oldDiscogsUrl) {
            this.offerDiscogsSync();
          }
        } else if (r.status === 409) {
          this.editMessage = 'Error: Album with that artist/name already exists';
        } else {
          this.editMessage = 'Error saving changes';
        }
      } catch (e) {
        this.editMessage = 'Error saving changes';
      }
    },

    async offerDiscogsSync() {
      if (!confirm('Discogs link updated. Update track metadata (side, position) from Discogs?')) return;
      this.editMessage = 'Fetching from Discogs...';
      try {
        const r = await fetch(`/albums/${this.albumDetail.album_id}/apply-discogs`, { method: 'POST' });
        if (r.ok) {
          const data = await r.json();
          this.editMessage = `Updated ${data.updated_count} track(s) from Discogs`;
          await this.openAlbum(this.albumDetail.album_id);
        } else {
          const err = await r.json().catch(() => ({}));
          this.editMessage = `Discogs sync failed: ${err.detail || 'unknown error'}`;
        }
      } catch (e) {
        this.editMessage = 'Discogs sync failed: network error';
      }
    },

    async uploadCover(event) {
      const file = event.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      try {
        await fetch(`/albums/${this.albumDetail.album_id}/cover`, {
          method: 'POST',
          body: form,
        });
        await this.openAlbum(this.albumDetail.album_id);
        await this.loadAlbums();
      } catch (e) {
        console.error('Failed to upload cover:', e);
      }
    },

    async deleteAlbum() {
      if (!confirm(`Delete "${this.albumDetail.name}" and all its tracks?`)) return;
      try {
        await fetch(`/albums/${this.albumDetail.album_id}`, { method: 'DELETE' });
        this.view = 'library';
        await this.loadAlbums();
      } catch (e) {
        console.error('Failed to delete album:', e);
      }
    },

    openTrackEdit(track) {
      this.editingTrack = track;
      this.trackEditForm = {
        track: track.track,
        track_number: track.track_number,
        side: track.side,
        position: track.position,
      };
    },

    async saveTrack() {
      if (!this.editingTrack) return;
      try {
        await fetch(`/tracks/${this.editingTrack.track_id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.trackEditForm),
        });
        this.editingTrack = null;
        await this.openAlbum(this.albumDetail.album_id);
      } catch (e) {
        console.error('Failed to save track:', e);
      }
    },

    async deleteTrack(trackId) {
      if (!confirm('Delete this track?')) return;
      this.deletingTracks.add(trackId);
      this.deletingTracks = new Set(this.deletingTracks);
      try {
        await fetch(`/tracks/${trackId}`, { method: 'DELETE' });
      } catch (e) {
        console.error('Failed to delete track:', e);
      }
      this.deletingTracks.delete(trackId);
      this.deletingTracks = new Set(this.deletingTracks);
      await this.openAlbum(this.albumDetail.album_id);
    },

    async addTracksToAlbum(files) {
      if (!files.length || !this.albumDetail) return;
      this.addingTracks = true;
      this.addTracksTotal = files.length;
      this.addTracksProgress = 0;

      for (const file of files) {
        this.addTracksProgress++;
        const trackName = file.name.replace(/\.[^.]+$/, '');
        const metadata = {
          album_id: this.albumDetail.album_id,
          artist: this.albumDetail.artist,
          album: this.albumDetail.name,
          track: trackName,
        };
        const form = new FormData();
        form.append('file', file);
        form.append('metadata', JSON.stringify(metadata));
        try {
          await fetch('/ingest', { method: 'POST', body: form });
        } catch (e) {
          console.error(`Failed to ingest ${file.name}:`, e);
        }
      }

      this.addingTracks = false;
      await this.openAlbum(this.albumDetail.album_id);
    },

    async loadSettings() {
      try {
        const r = await fetch('/settings');
        if (r.ok) {
          this.settingsForm = await r.json();
        }
      } catch (e) {
        console.error('Failed to load settings:', e);
      }
      this.settingsMessage = '';
    },

    async saveSettings() {
      this.settingsSaving = true;
      this.settingsMessage = '';
      try {
        const r = await fetch('/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.settingsForm),
        });
        if (r.ok) {
          this.settingsMessage = 'Settings saved!';
        } else {
          this.settingsMessage = 'Error saving settings';
        }
      } catch (e) {
        this.settingsMessage = 'Error saving settings';
      }
      this.settingsSaving = false;
    },

    formatDuration(seconds) {
      if (!seconds) return '';
      const m = Math.floor(seconds / 60);
      const s = Math.floor(seconds % 60);
      return `${m}:${s.toString().padStart(2, '0')}`;
    },

    handleDrop(event) {
      this.dragOver = false;
      this.handleFiles(event.dataTransfer.files);
    },

    async handleFiles(files) {
      if (!files.length) return;
      this.uploading = true;
      this.uploadProgress = 0;
      this.uploadResult = null;

      const form = new FormData();
      for (const f of files) {
        form.append('files', f);
      }

      const xhr = new XMLHttpRequest();
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          this.uploadProgress = Math.round((e.loaded / e.total) * 100);
        }
      };
      xhr.onload = () => {
        this.uploading = false;
        try {
          this.uploadResult = JSON.parse(xhr.responseText);
        } catch (e) {
          this.uploadResult = {
            albums_created: 0,
            tracks_ingested: 0,
            errors: [{ file: 'upload', error: 'Invalid server response' }],
          };
        }
        this.loadAlbums();
      };
      xhr.onerror = () => {
        this.uploading = false;
        this.uploadResult = {
          albums_created: 0,
          tracks_ingested: 0,
          errors: [{ file: 'upload', error: 'Network error' }],
        };
      };
      xhr.open('POST', '/ingest/bulk');
      xhr.send(form);
    },
  };
}
