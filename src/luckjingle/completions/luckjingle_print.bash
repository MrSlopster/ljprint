# bash completion for luckjingle-print / ljprint
#
# Install (one of):
#   1. cp this file to /etc/bash_completion.d/luckjingle-print
#   2. cp this file to ~/.local/share/bash-completion/completions/luckjingle-print
#   3. source this file from your ~/.bashrc
#   4. eval "$(uv run luckjingle-print completions --shell bash)"
#
# MAC addresses are completed from the BlueZ device cache (`bluetoothctl devices`).
# Pair the printer once with `bluetoothctl pair <mac>` to populate that cache.

# Cache of known BlueZ devices, refreshed at most every 2 seconds during a
# single Tab completion burst.
_luckjingle_print_mac_cache_ts=0
_luckjingle_print_mac_cache=""
_luckjingle_print_macs() {
    local now=$(date +%s) 2>/dev/null
    if [ $((now - _luckjingle_print_mac_cache_ts)) -ge 2 ]; then
        _luckjingle_print_mac_cache=$(
            bluetoothctl devices 2>/dev/null | awk '/^Device/ {print $2}')
        _luckjingle_print_mac_cache_ts=$now
    fi
    printf '%s\n' "$_luckjingle_print_mac_cache"
}

_luckjingle_print() {
    # Manual parse — don't rely on _init_completion, which misbehaves when
    # invoked outside an interactive Tab context (leaves $cur empty).
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    cword=$COMP_CWORD
    words=("${COMP_WORDS[@]}")

    local subcommands="scan gatt-map raw info status watch
        print-text print print-image print-pdf print-qr print-barcode print-grid
        set-density set-speed set-paper-type set-heating set-shuttime set-width set-time reset
        completions"

    local print_common="--mode --force"
    local modes="normal tattoo water-transfer a4"
    local aligns="left center right"
    local dithers="floyd threshold none"
    local styles="grid ruled lined"
    # Common python-barcode types
    local barcodes="code128 code39 code93 ean13 ean8 ean14 isbn isbn13 issn jan upc upca gs1"
    local numeric_args="--duration --interval --threshold --font-size --box-size
        --rows --cols --line-spacing --level --speed --heating --minutes
        --pixels --kind --mask --pages"

    # 1) First positional after the binary = subcommand name
    if [ "$cword" -eq 1 ]; then
        COMPREPLY=($(compgen -W "$subcommands -h --help -v --verbose" -- "$cur"))
        return 0
    fi

    local cmd="${words[1]}"

    # 2) Complete option values (the word right after a --flag)
    case "$prev" in
        --mode)
            COMPREPLY=($(compgen -W "$modes" -- "$cur")); return 0 ;;
        --align)
            COMPREPLY=($(compgen -W "$aligns" -- "$cur")); return 0 ;;
        --dither)
            COMPREPLY=($(compgen -W "$dithers" -- "$cur")); return 0 ;;
        --style)
            COMPREPLY=($(compgen -W "$styles" -- "$cur")); return 0 ;;
        --shell)
            COMPREPLY=($(compgen -W "bash zsh" -- "$cur")); return 0 ;;
        --btype)
            COMPREPLY=($(compgen -W "$barcodes" -- "$cur")); return 0 ;;
    esac
    # Numeric option: no completion (user types a number)
    for opt in $numeric_args; do
        if [ "$prev" = "$opt" ]; then return 0; fi
    done

    # 3) Completing a flag (starts with -)
    if [[ "$cur" == -* ]]; then
        local opts=""
        case "$cmd" in
            scan)           opts="--duration" ;;
            gatt-map|info|status|reset|set-time) opts="" ;;
            watch)          opts="--interval" ;;
            raw)            opts="" ;;
            print-text|print)
                opts="$print_common --font-size --bold --align" ;;
            print-image)
                opts="$print_common --dither --threshold" ;;
            print-pdf)
                opts="$print_common --pages --dither --threshold" ;;
            print-qr)
                opts="$print_common --box-size" ;;
            print-barcode)
                opts="$print_common" ;;
            print-grid)
                opts="$print_common --style --rows --cols --line-spacing" ;;
            set-density)    opts="" ;;
            set-speed)      opts="" ;;
            set-paper-type) opts="" ;;
            set-heating)    opts="" ;;
            set-shuttime)   opts="" ;;
            set-width)      opts="" ;;
            completions)    opts="--shell" ;;
            *)              opts="" ;;
        esac
        COMPREPLY=($(compgen -W "$opts -h --help" -- "$cur"))
        return 0
    fi

    # 4) Positional arguments
    # Count positionals already filled (skip flags and their values).
    local pos_idx=0
    local i=2
    while [ $i -lt $cword ]; do
        local w="${words[i]}"
        if [[ "$w" == -* ]]; then
            # Skip the value of a non-numeric-value flag too
            case "$w" in
                --mode|--align|--dither|--style|--shell|--btype)
                    i=$((i+1)) ;;
            esac
        else
            pos_idx=$((pos_idx+1))
        fi
        i=$((i+1))
    done

    case "$cmd" in
        scan|completions)
            # No MAC needed
            return 0
            ;;
        print-barcode)
            # Position 0 = btype, position 1 = data
            if [ $pos_idx -eq 0 ]; then
                COMPREPLY=($(compgen -W "$barcodes" -- "$cur"))
                return 0
            fi
            return 0
            ;;
        print-text|print|print-qr)
            # Position 0 = mac, position 1 = text/data
            if [ $pos_idx -eq 0 ]; then
                COMPREPLY=($(compgen -W "$(_luckjingle_print_macs)" -- "$cur"))
                return 0
            fi
            return 0
            ;;
        print-image|print-pdf)
            # Position 0 = mac, position 1 = file -> default file completion
            if [ $pos_idx -eq 0 ]; then
                COMPREPLY=($(compgen -W "$(_luckjingle_print_macs)" -- "$cur"))
                return 0
            fi
            # Fall through to default filename completion
            ;;
        raw)
            # Position 0 = mac, position 1 = hex
            if [ $pos_idx -eq 0 ]; then
                COMPREPLY=($(compgen -W "$(_luckjingle_print_macs)" -- "$cur"))
                return 0
            fi
            return 0
            ;;
        *)
            # All other commands: first positional is MAC
            if [ $pos_idx -eq 0 ]; then
                COMPREPLY=($(compgen -W "$(_luckjingle_print_macs)" -- "$cur"))
                return 0
            fi
            ;;
    esac

    return 0
}

# Register for every invocation name: the canonical hyphenated binary, the
# short alias, and (legacy) the underscore form for users still running the
# pre-migration entry point.
complete -F _luckjingle_print luckjingle-print
complete -F _luckjingle_print ljprint
complete -F _luckjingle_print luckjingle_print
