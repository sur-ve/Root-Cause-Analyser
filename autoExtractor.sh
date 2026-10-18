#!/bin/bash
arguments=("cpuStressor1" "cpuStressor2" "cpuStressor3" "memStressor1" "memStressor2" "memStressor3" "lockStressor1" "lockStressor2" "ioStressor1" "ioStressor2" "default")

if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
else
    SUDO=""
fi

for arg in "${arguments[@]}"; do
    echo "===================================================="
    echo "Running parentProcess.py with argument: $arg"
    echo "==================================================="

    $SUDO python3 parentProcess.py "$arg"

    if [ $? -ne 0 ]; then
        echo "Error: parentProcess.py failed with argument '$arg'. Aborting remaining runs."
        exit 1
    fi
done

echo "All runs completed successfully!"