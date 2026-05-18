# sentry-taskbroker-management
Libraries for managing Taskbroker.


### Usage
- `make install-dev` to install the development environment
- `make tests` to run the unit tests
- `make typecheck` to run Python type checking
- `make lint` to lint the code base and apply auto generated changes
- `make build` to build the wheels for the project and package the release

### Docker

Build the image:

```bash
docker build -t sentry-taskbroker-management:local .
```

Show CLI help (router):

```bash
docker run --rm sentry-taskbroker-management:local --help
```
