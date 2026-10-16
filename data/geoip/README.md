# Local country database

Place the licensed `GeoLite2-Country.mmdb` release in this directory on the
server. The API reads only the two-letter country code in memory, does not call
MaxMind's web service, and never stores or logs the source IP.

Keep the MMDB out of Git and refresh it through the server's normal secret and
data provisioning process.
